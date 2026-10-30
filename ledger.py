"""
Public ledger: signed JSON receipts for every transaction.

For every transaction the bot writes one file:

    {LEDGER_ROOT}/transactions/{id}.json

The file is signed with the node's Ed25519 key (see `signing.py`). The
ledger is a mirror of the SQLite DB, not the source of truth — if a
write here fails, the transaction has already happened and the bot must
not retry from this layer. We log and move on.

Two public entry points:

  write_local_transaction_to_ledger(tx_row, ledger_root=None)
      — called once per successful transfer from bot.py

  backfill_local_transactions(db_path, ledger_root)
      — one-shot rebuild of every existing transaction in the DB

Both are idempotent. If a file with the same id already exists, we
re-write it (the canonical form + signature will match byte-for-byte
unless the underlying row changed).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import config
import signing

logger = logging.getLogger("ubi-bot.ledger")

LEDGER_FORMAT_VERSION = "1.1"  # 1.1: blue_pct dropped (one kind of time, 2026-10-30)
TX_TYPE_TRANSFER = "transfer"


# ---------------------------------------------------------------------------
# Canonicalisation helpers
# ---------------------------------------------------------------------------

def _to_iso_utc(ts: str) -> str:
    """Convert SQLite's `YYYY-MM-DD HH:MM:SS` (UTC) to ISO 8601 with Z suffix."""
    # SQLite datetime('now') is UTC by spec.
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        # Already ISO? Try fromisoformat.
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_ledger_root(override: Optional[str]) -> Optional[Path]:
    root = override or config.LEDGER_ROOT
    if not root:
        return None
    return Path(root).expanduser()


# ---------------------------------------------------------------------------
# Build + write a single entry
# ---------------------------------------------------------------------------

def build_local_transaction_entry(
    *,
    tx_id: int,
    sender_handle: str,
    recipient_handle: str,
    amount_seconds: int,
    created_at: str,
    node_domain: Optional[str] = None,
) -> dict:
    """Build the unsigned ledger entry for a local-only transaction.

    `from_node == to_node` for local transactions. Federated transfers
    (Stage 2b) will reuse this shape with the remote node domain in one
    of the two fields.
    """
    if node_domain is None:
        node_domain = config.LOCAL_NODE_DOMAIN

    return {
        "ledger_format_version": LEDGER_FORMAT_VERSION,
        "id": f"tx_{tx_id:08d}",
        "type": TX_TYPE_TRANSFER,
        "from_handle": sender_handle,
        "to_handle": recipient_handle,
        "from_node": node_domain,
        "to_node": node_domain,
        "amount_seconds": int(amount_seconds),
        "timestamp_iso": _to_iso_utc(created_at),
    }


def sign_and_write_entry(entry: dict, ledger_root: Path) -> Path:
    """Sign `entry`, set node-stamp fields, and write to disk.

    Returns the path of the written file.
    """
    kp = signing.load_or_create_keypair()

    entry["signed_at_iso"] = _now_iso()
    entry["signing_node_domain"] = config.LOCAL_NODE_DOMAIN
    entry["signing_node_public_key_fingerprint"] = kp["fingerprint"]

    # Sign over the canonical form (which excludes node_signature).
    entry["node_signature"] = signing.sign_entry(entry, kp["signing_key"])

    tx_dir = ledger_root / "transactions"
    tx_dir.mkdir(parents=True, exist_ok=True)

    path = tx_dir / f"{entry['id']}.json"
    # Atomic write: tmp then rename.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entry, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return path


def write_local_transaction_to_ledger(
    *,
    tx_id: int,
    sender_handle: str,
    recipient_handle: str,
    amount_seconds: int,
    created_at: str,
    ledger_root: Optional[str] = None,
) -> Optional[Path]:
    """Public entry point called from bot.py after a successful transfer.

    Returns the written Path, or None if ledger writes are disabled
    (LEDGER_ROOT unset). All exceptions are caught and logged — the
    caller already committed the DB row, the ledger must never fail
    upstream.
    """
    root = _resolve_ledger_root(ledger_root)
    if root is None:
        logger.info("LEDGER_ROOT not set — skipping ledger write for tx %s", tx_id)
        return None

    try:
        entry = build_local_transaction_entry(
            tx_id=tx_id,
            sender_handle=sender_handle,
            recipient_handle=recipient_handle,
            amount_seconds=amount_seconds,
            created_at=created_at,
        )
        path = sign_and_write_entry(entry, root)
        logger.info("Ledger entry written: %s", path)
        return path
    except Exception as exc:  # pylint: disable=broad-except
        # Ledger is a mirror; never propagate.
        logger.error("Ledger write failed for tx %s: %s", tx_id, exc, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------

def backfill_local_transactions(db_path: str, ledger_root: str) -> dict:
    """One-shot rebuild of every existing transaction in the DB.

    Idempotent: re-running produces byte-identical files (signatures
    will differ because `signed_at_iso` is current-time; see note below).
    Use the `--skip-existing` option in the script wrapper to avoid
    that.

    Returns a summary dict with counts.
    """
    root = Path(ledger_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    rows = cur.execute(
        """
        SELECT t.id          AS tx_id,
               t.amount      AS amount,
               t.created_at  AS created_at,
               s.handle_display AS sender_handle,
               r.handle_display AS recipient_handle
        FROM transactions t
        JOIN users s ON t.sender_id = s.id
        JOIN users r ON t.recipient_id = r.id
        ORDER BY t.id
        """
    ).fetchall()
    con.close()

    written = 0
    skipped = 0
    for row in rows:
        path = root / "transactions" / f"tx_{row['tx_id']:08d}.json"
        if path.exists():
            skipped += 1
            continue
        entry = build_local_transaction_entry(
            tx_id=row["tx_id"],
            sender_handle=row["sender_handle"],
            recipient_handle=row["recipient_handle"],
            amount_seconds=row["amount"],
            created_at=row["created_at"],
        )
        sign_and_write_entry(entry, root)
        written += 1

    return {
        "total_rows": len(rows),
        "written": written,
        "skipped_existing": skipped,
        "ledger_root": str(root),
    }


# ---------------------------------------------------------------------------
# CLI for one-off backfill
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="UBI ledger backfill")
    parser.add_argument("--db", default=config.DB_PATH, help="path to ubi_bot.db")
    parser.add_argument(
        "--ledger-root",
        default=config.LEDGER_ROOT,
        help="path to the ledger root (default: $LEDGER_ROOT)",
    )
    args = parser.parse_args()

    if not args.ledger_root:
        raise SystemExit("LEDGER_ROOT not set and --ledger-root not given")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
    result = backfill_local_transactions(args.db, args.ledger_root)
    print(json.dumps(result, indent=2))
