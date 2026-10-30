"""
Time coupons — the way time is sent (issue 02, Stefano 2026-10-30).

A coupon is a signed, self-contained receipt from the issuer's node:

  - hours leave the issuer's Wallet (then Vault) when the coupon is issued
    (escrow), never to return to the issuer;
  - whoever opens the coupon first receives the time in their Vault
    (single redemption; the redeemer needs no Handle yet — the coupon
    doubles as an invite);
  - a coupon funded even partly from the Wallet expires at midnight UTC at
    the latest; a Vault-funded coupon may run longer;
  - unredeemed at expiry, the time flows to the Universal Circles.

This module owns the `coupons` table, the escrow/redeem/expire state
machine, and the signed JSON written under <LEDGER_ROOT>/coupons/. The same
signed object is what federation v2 carries across nodes.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

import config
from database import DB_PATH, CIRCLE_KEYS

try:
    import ledger as _ledger
    import signing as _signing
except Exception as _exc:  # pragma: no cover
    _ledger = None
    _signing = None
    logging.getLogger("ubi-bot.coupons").warning(
        "ledger/signing unavailable (%s); coupons will not be mirrored", _exc
    )

logger = logging.getLogger("ubi-bot.coupons")

COUPON_FORMAT_VERSION = "1.0"
STATUS_ISSUED = "issued"
STATUS_REDEEMED = "redeemed"
STATUS_EXPIRED = "expired"

# Longest validity a fully Vault-funded coupon may ask for.
MAX_VALIDITY_SECONDS = 30 * 86400


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def next_midnight_utc(now: Optional[datetime] = None) -> datetime:
    now = now or utcnow()
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sql(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _from_sql(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def new_coupon_id() -> str:
    """16 url-safe chars (~96 bits). Fits Telegram's start= parameter."""
    return secrets.token_urlsafe(12)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

async def init_coupons_schema() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS coupons (
                id              TEXT PRIMARY KEY,
                issuer_id       INTEGER NOT NULL REFERENCES users(id),
                amount          INTEGER NOT NULL,
                wallet_part     INTEGER NOT NULL DEFAULT 0,
                vault_part      INTEGER NOT NULL DEFAULT 0,
                lock_handle     TEXT,
                status          TEXT NOT NULL DEFAULT 'issued',
                issued_at       TEXT NOT NULL DEFAULT (datetime('now')),
                expires_at      TEXT NOT NULL,
                redeemed_by     INTEGER REFERENCES users(id),
                redeemed_at     TEXT,
                redeem_tx_id    INTEGER REFERENCES transactions(id),
                expired_at      TEXT
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_coupons_status_expiry ON coupons(status, expires_at)"
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Issue (escrow)
# ---------------------------------------------------------------------------

async def issue_coupon(
    issuer_telegram_id: int,
    amount: int,
    validity_seconds: Optional[int] = None,
    lock_handle: Optional[str] = None,
) -> dict:
    """
    Escrow `amount` seconds from the issuer and create a coupon.

    validity_seconds=None means "until midnight UTC". Any coupon that takes
    even one second from the Wallet is capped at midnight UTC regardless of
    what was asked; the result reports `capped_to_midnight=True` so the UI
    can say so.
    """
    if amount < 1:
        return {"success": False, "error": "amount_too_small"}

    now = utcnow()
    midnight = next_midnight_utc(now)
    requested = None
    if validity_seconds is not None:
        validity_seconds = max(1, min(int(validity_seconds), MAX_VALIDITY_SECONDS))
        requested = now + timedelta(seconds=validity_seconds)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (issuer_telegram_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return {"success": False, "error": "not_registered"}
        issuer = dict(row)

        wallet = issuer["daily_wallet"]
        vault = issuer["time_vault"]
        if amount > wallet + vault:
            return {
                "success": False,
                "error": "insufficient_balance",
                "available": wallet + vault,
            }

        wants_long = requested is not None and requested > midnight
        if wants_long:
            # A coupon meant to outlive midnight is funded from the Vault
            # first, so that Wallet time (which must die at midnight) is only
            # touched when the Vault cannot cover the amount.
            vault_part = min(amount, vault)
            wallet_part = amount - vault_part
        else:
            wallet_part = min(amount, wallet)
            vault_part = amount - wallet_part

        capped = False
        if requested is None:
            expires = midnight
        elif wants_long and wallet_part > 0:
            expires = midnight
            capped = True
        else:
            expires = min(requested, midnight) if wallet_part > 0 else requested

        coupon_id = new_coupon_id()
        await db.execute(
            "UPDATE users SET daily_wallet = ?, time_vault = ? WHERE id = ?",
            (wallet - wallet_part, vault - vault_part, issuer["id"]),
        )
        await db.execute(
            """
            INSERT INTO coupons (id, issuer_id, amount, wallet_part, vault_part,
                                 lock_handle, status, issued_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, 'issued', ?, ?)
            """,
            (coupon_id, issuer["id"], amount, wallet_part, vault_part,
             lock_handle, _sql(now), _sql(expires)),
        )
        await db.commit()

    coupon = {
        "id": coupon_id,
        "issuer_handle": issuer["handle_display"],
        "issuer_telegram_id": issuer["telegram_id"],
        "amount": amount,
        "wallet_part": wallet_part,
        "vault_part": vault_part,
        "lock_handle": lock_handle,
        "status": STATUS_ISSUED,
        "issued_at": now,
        "expires_at": expires,
    }
    signed = _write_signed_coupon(coupon)
    return {
        "success": True,
        "coupon": coupon,
        "signed": signed,
        "capped_to_midnight": capped,
        "issuer_wallet_remaining": wallet - wallet_part,
        "issuer_vault_remaining": vault - vault_part,
    }


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

async def get_coupon(coupon_id: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT c.*, u.handle_display AS issuer_handle, u.telegram_id AS issuer_telegram_id,
                   r.handle_display AS redeemer_handle
            FROM coupons c
            JOIN users u ON c.issuer_id = u.id
            LEFT JOIN users r ON c.redeemed_by = r.id
            WHERE c.id = ?
            """,
            (coupon_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def list_coupons_for_issuer(telegram_id: int, limit: int = 10) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT c.*, r.handle_display AS redeemer_handle
            FROM coupons c
            JOIN users u ON c.issuer_id = u.id
            LEFT JOIN users r ON c.redeemed_by = r.id
            WHERE u.telegram_id = ?
            ORDER BY c.issued_at DESC
            LIMIT ?
            """,
            (telegram_id, limit),
        )
        return [dict(r) for r in await cursor.fetchall()]


# ---------------------------------------------------------------------------
# Redeem
# ---------------------------------------------------------------------------

async def redeem_coupon(coupon_id: str, redeemer_telegram_id: int) -> dict:
    """
    Credit the coupon's time to the redeemer's Vault. Single redemption is
    enforced with a conditional UPDATE on status='issued'.
    """
    now = utcnow()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT c.*, u.handle_display AS issuer_handle, u.telegram_id AS issuer_telegram_id "
            "FROM coupons c JOIN users u ON c.issuer_id = u.id WHERE c.id = ?",
            (coupon_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return {"success": False, "error": "not_found"}
        coupon = dict(row)

        cursor = await db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (redeemer_telegram_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return {"success": False, "error": "not_registered", "coupon": coupon}
        redeemer = dict(row)

        if coupon["status"] != STATUS_ISSUED:
            return {"success": False, "error": coupon["status"], "coupon": coupon}
        if _from_sql(coupon["expires_at"]) <= now:
            return {"success": False, "error": "expired", "coupon": coupon}
        if redeemer["id"] == coupon["issuer_id"]:
            # Self-redemption would turn a Wallet coupon into a Vault deposit.
            return {"success": False, "error": "own_coupon", "coupon": coupon}
        if coupon["lock_handle"] and coupon["lock_handle"] != redeemer["handle_display"]:
            return {"success": False, "error": "locked", "coupon": coupon}

        amount = coupon["amount"]
        new_vault = redeemer["time_vault"] + amount
        overflow = 0
        if new_vault > redeemer["vault_capacity"]:
            overflow = new_vault - redeemer["vault_capacity"]
            new_vault = redeemer["vault_capacity"]

        cursor = await db.execute(
            "UPDATE coupons SET status = 'redeemed', redeemed_by = ?, redeemed_at = ? "
            "WHERE id = ? AND status = 'issued'",
            (redeemer["id"], _sql(now), coupon_id),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            return {"success": False, "error": "redeemed", "coupon": coupon}

        await db.execute(
            "UPDATE users SET time_vault = ? WHERE id = ?", (new_vault, redeemer["id"])
        )
        if overflow > 0:
            await _credit_circles(db, overflow)

        cursor = await db.execute(
            "INSERT INTO transactions (sender_id, recipient_id, amount, wallet_part, vault_part) "
            "VALUES (?, ?, ?, ?, ?)",
            (coupon["issuer_id"], redeemer["id"], amount, coupon["wallet_part"], coupon["vault_part"]),
        )
        tx_id = cursor.lastrowid
        await db.execute(
            "UPDATE coupons SET redeem_tx_id = ? WHERE id = ?", (tx_id, coupon_id)
        )
        await db.commit()

    coupon.update(
        status=STATUS_REDEEMED,
        redeemed_by=redeemer["id"],
        redeemed_at=_sql(now),
        redeemer_handle=redeemer["handle_display"],
        redeem_tx_id=tx_id,
    )
    _write_ledger_tx(
        tx_id=tx_id,
        tx_type="coupon_redeem",
        from_handle=coupon["issuer_handle"],
        to_handle=redeemer["handle_display"],
        amount=amount,
        created_at=_sql(now),
        coupon_id=coupon_id,
    )
    _write_signed_coupon_status(coupon)
    return {
        "success": True,
        "coupon": coupon,
        "amount": amount,
        "overflow": overflow,
        "redeemer_vault_new": new_vault,
        "tx_id": tx_id,
    }


# ---------------------------------------------------------------------------
# Expire
# ---------------------------------------------------------------------------

async def expire_due_coupons() -> list[dict]:
    """
    Move every coupon past its expiry to the Universal Circles.
    Safe to call as often as you like; each coupon is handled once.
    """
    now = utcnow()
    expired: list[dict] = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT c.*, u.handle_display AS issuer_handle, u.telegram_id AS issuer_telegram_id "
            "FROM coupons c JOIN users u ON c.issuer_id = u.id "
            "WHERE c.status = 'issued' AND c.expires_at <= ?",
            (_sql(now),),
        )
        due = [dict(r) for r in await cursor.fetchall()]
        for c in due:
            cursor = await db.execute(
                "UPDATE coupons SET status = 'expired', expired_at = ? "
                "WHERE id = ? AND status = 'issued'",
                (_sql(now), c["id"]),
            )
            if cursor.rowcount != 1:
                continue
            await _credit_circles(db, c["amount"])
            c["status"] = STATUS_EXPIRED
            c["expired_at"] = _sql(now)
            expired.append(c)
        await db.commit()

    for c in expired:
        _write_signed_coupon_status(c)
        _write_ledger_tx(
            tx_id=None,
            tx_type="coupon_expired",
            from_handle=c["issuer_handle"],
            to_handle="universal:circles:all",
            amount=c["amount"],
            created_at=c["expired_at"],
            coupon_id=c["id"],
        )
    if expired:
        logger.info("Expired %d coupon(s) to the Universal Circles", len(expired))
    return expired


async def _credit_circles(db: aiosqlite.Connection, seconds: int) -> None:
    """Split `seconds` equally across the five Circles; remainder to the first."""
    share, remainder = divmod(seconds, len(CIRCLE_KEYS))
    for i, key in enumerate(CIRCLE_KEYS):
        await db.execute(
            "UPDATE universal_circles_pool "
            "SET total_seconds = total_seconds + ?, last_updated = datetime('now') "
            "WHERE circle_key = ?",
            (share + (remainder if i == 0 else 0), key),
        )


# ---------------------------------------------------------------------------
# Signed objects on disk
# ---------------------------------------------------------------------------

def _coupon_dir() -> Optional[Path]:
    if not config.LEDGER_ROOT or _signing is None:
        return None
    d = Path(config.LEDGER_ROOT).expanduser() / "coupons"
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_coupon_object(coupon: dict) -> dict:
    """The signed, self-contained coupon. Federation carries this object."""
    issued = coupon["issued_at"]
    expires = coupon["expires_at"]
    return {
        "coupon_format_version": COUPON_FORMAT_VERSION,
        "id": f"cpn_{coupon['id']}",
        "type": "coupon",
        "issuer_handle": coupon["issuer_handle"],
        "issuer_node": config.LOCAL_NODE_DOMAIN,
        "amount_seconds": int(coupon["amount"]),
        "lock_handle": coupon.get("lock_handle"),
        "issued_at_iso": _iso(issued) if isinstance(issued, datetime) else _iso(_from_sql(issued)),
        "expires_at_iso": _iso(expires) if isinstance(expires, datetime) else _iso(_from_sql(expires)),
    }


def _sign_and_write(obj: dict, path: Path) -> None:
    kp = _signing.load_or_create_keypair()
    obj["signed_at_iso"] = _iso(utcnow())
    obj["signing_node_domain"] = config.LOCAL_NODE_DOMAIN
    obj["signing_node_public_key_fingerprint"] = kp["fingerprint"]
    obj["node_signature"] = _signing.sign_entry(obj, kp["signing_key"])
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def _write_signed_coupon(coupon: dict) -> Optional[dict]:
    d = _coupon_dir()
    if d is None:
        return None
    try:
        obj = build_coupon_object(coupon)
        _sign_and_write(obj, d / f"cpn_{coupon['id']}.json")
        return obj
    except Exception as exc:
        logger.error("Could not write signed coupon %s: %s", coupon["id"], exc)
        return None


def _write_signed_coupon_status(coupon: dict) -> None:
    """A second signed file records the outcome; the issuance file is never rewritten."""
    d = _coupon_dir()
    if d is None:
        return
    try:
        obj = {
            "coupon_format_version": COUPON_FORMAT_VERSION,
            "id": f"cpn_{coupon['id']}.{coupon['status']}",
            "type": f"coupon_{coupon['status']}",
            "coupon_id": f"cpn_{coupon['id']}",
            "issuer_handle": coupon["issuer_handle"],
            "amount_seconds": int(coupon["amount"]),
            "redeemer_handle": coupon.get("redeemer_handle"),
            "at_iso": _iso(_from_sql(coupon.get("redeemed_at") or coupon.get("expired_at"))),
        }
        _sign_and_write(obj, d / f"{obj['id']}.json")
    except Exception as exc:
        logger.error("Could not write coupon status %s: %s", coupon["id"], exc)


def _write_ledger_tx(
    *,
    tx_id: Optional[int],
    tx_type: str,
    from_handle: str,
    to_handle: str,
    amount: int,
    created_at: str,
    coupon_id: str,
) -> None:
    """Mirror a coupon outcome into the public transactions ledger."""
    if _ledger is None or not config.LEDGER_ROOT:
        return
    try:
        entry = {
            "ledger_format_version": _ledger.LEDGER_FORMAT_VERSION,
            "id": f"tx_{tx_id:08d}" if tx_id is not None else f"cpx_{coupon_id}",
            "type": tx_type,
            "coupon_id": f"cpn_{coupon_id}",
            "from_handle": from_handle,
            "to_handle": to_handle,
            "from_node": config.LOCAL_NODE_DOMAIN,
            "to_node": config.LOCAL_NODE_DOMAIN,
            "amount_seconds": int(amount),
            "timestamp_iso": _iso(_from_sql(created_at)),
        }
        _ledger.sign_and_write_entry(entry, Path(config.LEDGER_ROOT).expanduser())
    except Exception as exc:
        logger.error("Could not write ledger entry for coupon %s: %s", coupon_id, exc)
