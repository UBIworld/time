#!/usr/bin/env python3
"""
Migration 002 — federation schema (architecture-agnostic).

Adds:
  * users.node_domain  (TEXT, nullable; backfilled with LOCAL_NODE_DOMAIN)
  * users.is_local     (INTEGER NOT NULL DEFAULT 1; set to 1 for all existing
                        rows — every pre-federation user is local by definition)
  * peer_nodes         (new table: known federation peers)
  * federated_transactions
                       (new table: cross-node transfers, separate from local
                        transactions because they have different lifecycle
                        and consistency guarantees)
  * indexes            (peer_nodes.domain UNIQUE, fed_tx.idempotency_key UNIQUE,
                        fed_tx.local_user_id, fed_tx.status)

What this script does:
  1. Reads LOCAL_NODE_DOMAIN from the .env next to this repo (or the
     environment), defaulting to "localhost" if unset. This is what every
     existing user's node_domain will be set to.
  2. Inspects the schema. If columns/tables/indexes are already present
     (i.e. the bot's init_db has already run after the change), the script
     is a no-op for that piece.
  3. Adds missing columns via ALTER TABLE.
  4. Creates missing tables via CREATE TABLE IF NOT EXISTS.
  5. Creates missing indexes via CREATE INDEX IF NOT EXISTS.
  6. Backfills users.node_domain where NULL and users.is_local where it's
     not set yet (the ALTER's default takes care of newly-added column rows,
     but we re-assert defensively).
  7. Prints a before/after summary so the operator can sanity-check.
  8. Runs inside a single transaction. Any error rolls back the whole thing.

Idempotent: safe to run multiple times. The second run reports zero changes.

Usage:
    python3 migrations/002_federation_schema.py
    python3 migrations/002_federation_schema.py /path/to/db
    python3 migrations/002_federation_schema.py --dry-run

Exit codes:
    0  success (including the idempotent no-op case)
    1  failure (rolled back)
    2  bad arguments / DB not found

Constraint: this script does NOT touch the live production DB unless an
operator runs it explicitly. The bot's init_db will also create these
schema elements on startup for fresh installs, but ONLY this migration
populates `node_domain` for pre-existing user rows from .env.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path


DEFAULT_DB_PATH = "ubi_bot.db"


def _read_local_node_domain() -> str:
    """
    Resolve LOCAL_NODE_DOMAIN the same way config.py does:
      1. process environment first
      2. .env file next to the repo root (one directory above this file)
      3. fall back to "localhost"

    We don't import config.py because that imports BOT_TOKEN, which raises
    KeyError at import time when BOT_TOKEN is unset — and an operator
    running a schema migration shouldn't need a bot token.
    """
    if "LOCAL_NODE_DOMAIN" in os.environ:
        return os.environ["LOCAL_NODE_DOMAIN"]

    # The repo root is the parent of this migrations/ directory.
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        try:
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key == "LOCAL_NODE_DOMAIN" and value:
                    return value
        except OSError:
            # Permissions / read errors fall through to the default.
            pass

    return "localhost"


def _column_names(cur: sqlite3.Cursor, table: str) -> set[str]:
    cur.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}


def _table_exists(cur: sqlite3.Cursor, table: str) -> bool:
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return cur.fetchone() is not None


def _index_exists(cur: sqlite3.Cursor, index: str) -> bool:
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (index,)
    )
    return cur.fetchone() is not None


def _row_count(cur: sqlite3.Cursor, table: str) -> int:
    if not _table_exists(cur, table):
        return 0
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    return cur.fetchone()[0]


def _summary(cur: sqlite3.Cursor) -> dict:
    """Snapshot the schema state for before/after comparison."""
    users_cols = _column_names(cur, "users") if _table_exists(cur, "users") else set()
    return {
        "users_row_count":            _row_count(cur, "users"),
        "users_has_node_domain":      "node_domain" in users_cols,
        "users_has_is_local":         "is_local"    in users_cols,
        "users_with_node_domain_set": _users_with_node_domain(cur),
        "peer_nodes_exists":          _table_exists(cur, "peer_nodes"),
        "peer_nodes_row_count":       _row_count(cur, "peer_nodes"),
        "fed_tx_exists":              _table_exists(cur, "federated_transactions"),
        "fed_tx_row_count":           _row_count(cur, "federated_transactions"),
        "idx_peer_nodes_domain":      _index_exists(cur, "idx_peer_nodes_domain"),
        "idx_fed_tx_idempotency":     _index_exists(cur, "idx_fed_tx_idempotency"),
        "idx_fed_tx_local_user":      _index_exists(cur, "idx_fed_tx_local_user"),
        "idx_fed_tx_status":          _index_exists(cur, "idx_fed_tx_status"),
    }


def _users_with_node_domain(cur: sqlite3.Cursor) -> int:
    """How many rows have a non-NULL node_domain. -1 if column doesn't exist."""
    if not _table_exists(cur, "users"):
        return 0
    cols = _column_names(cur, "users")
    if "node_domain" not in cols:
        return -1
    cur.execute("SELECT COUNT(*) FROM users WHERE node_domain IS NOT NULL")
    return cur.fetchone()[0]


def _print_summary(label: str, s: dict) -> None:
    print(f"--- {label} ---")
    print(f"  users rows:                    {s['users_row_count']}")
    print(f"  users.node_domain column:      {'present' if s['users_has_node_domain'] else 'MISSING'}")
    print(f"  users.is_local column:         {'present' if s['users_has_is_local'] else 'MISSING'}")
    if s["users_with_node_domain_set"] == -1:
        print(f"  users with node_domain set:    n/a (column missing)")
    else:
        print(f"  users with node_domain set:    {s['users_with_node_domain_set']}")
    print(f"  peer_nodes table:              {'present' if s['peer_nodes_exists'] else 'MISSING'}  (rows: {s['peer_nodes_row_count']})")
    print(f"  federated_transactions table:  {'present' if s['fed_tx_exists'] else 'MISSING'}  (rows: {s['fed_tx_row_count']})")
    print(f"  index idx_peer_nodes_domain:   {'present' if s['idx_peer_nodes_domain'] else 'MISSING'}")
    print(f"  index idx_fed_tx_idempotency:  {'present' if s['idx_fed_tx_idempotency'] else 'MISSING'}")
    print(f"  index idx_fed_tx_local_user:   {'present' if s['idx_fed_tx_local_user'] else 'MISSING'}")
    print(f"  index idx_fed_tx_status:       {'present' if s['idx_fed_tx_status'] else 'MISSING'}")


def run_migration(db_path: str, dry_run: bool = False) -> int:
    p = Path(db_path)
    if not p.exists():
        print(f"ERROR: database file not found: {db_path}", file=sys.stderr)
        return 2

    local_node_domain = _read_local_node_domain()

    print("Migration 002 — federation schema (architecture-agnostic)")
    print(f"Database:          {db_path}")
    print(f"LOCAL_NODE_DOMAIN: {local_node_domain}")
    print(f"Dry run:           {dry_run}")
    print("-" * 60)

    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")

        before = _summary(cur)
        _print_summary("BEFORE", before)
        print()

        cur.execute("BEGIN")

        actions: list[str] = []

        # ---- users.node_domain ------------------------------------------------
        users_cols = _column_names(cur, "users")
        if "node_domain" not in users_cols:
            cur.execute("ALTER TABLE users ADD COLUMN node_domain TEXT")
            actions.append("ALTER users ADD node_domain TEXT")

        # ---- users.is_local --------------------------------------------------
        users_cols = _column_names(cur, "users")  # refresh
        if "is_local" not in users_cols:
            cur.execute(
                "ALTER TABLE users ADD COLUMN is_local INTEGER NOT NULL DEFAULT 1"
            )
            actions.append("ALTER users ADD is_local INTEGER NOT NULL DEFAULT 1")

        # ---- backfill node_domain --------------------------------------------
        # Any row whose node_domain is NULL gets the local node's domain.
        cur.execute(
            "UPDATE users SET node_domain = ? WHERE node_domain IS NULL",
            (local_node_domain,),
        )
        if cur.rowcount > 0:
            actions.append(
                f"UPDATE users SET node_domain = {local_node_domain!r} "
                f"WHERE node_domain IS NULL  -- {cur.rowcount} row(s)"
            )

        # ---- backfill is_local -----------------------------------------------
        # The DEFAULT 1 on the ALTER above handles new column adds, but be
        # defensive: assert every existing row is is_local = 1, since all
        # pre-federation users are local by definition.
        cur.execute("UPDATE users SET is_local = 1 WHERE is_local IS NULL OR is_local != 1")
        if cur.rowcount > 0:
            actions.append(
                f"UPDATE users SET is_local = 1 WHERE != 1  -- {cur.rowcount} row(s)"
            )

        # ---- peer_nodes ------------------------------------------------------
        if not _table_exists(cur, "peer_nodes"):
            cur.execute(
                """
                CREATE TABLE peer_nodes (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain          TEXT UNIQUE NOT NULL,
                    discovered_at   INTEGER NOT NULL,
                    last_seen_at    INTEGER,
                    status          TEXT NOT NULL DEFAULT 'active'
                                        CHECK (status IN ('active', 'defederated', 'unknown')),
                    public_key      TEXT,
                    metadata        TEXT
                )
                """
            )
            actions.append("CREATE TABLE peer_nodes")

        if not _index_exists(cur, "idx_peer_nodes_domain"):
            cur.execute(
                "CREATE INDEX idx_peer_nodes_domain ON peer_nodes(domain)"
            )
            actions.append("CREATE INDEX idx_peer_nodes_domain")

        # ---- federated_transactions -----------------------------------------
        if not _table_exists(cur, "federated_transactions"):
            cur.execute(
                """
                CREATE TABLE federated_transactions (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    local_user_id   INTEGER NOT NULL REFERENCES users(id),
                    remote_handle   TEXT NOT NULL,
                    direction       TEXT NOT NULL
                                        CHECK (direction IN ('out', 'in')),
                    amount_seconds  INTEGER NOT NULL
                                        CHECK (amount_seconds > 0),
                    blue_pct        INTEGER NOT NULL DEFAULT 100,
                    created_at      INTEGER NOT NULL,
                    confirmed_at    INTEGER,
                    reverted_at     INTEGER,
                    status          TEXT NOT NULL DEFAULT 'pending'
                                        CHECK (status IN ('pending', 'confirmed', 'failed', 'reverted')),
                    transport       TEXT NOT NULL,
                    external_id     TEXT,
                    idempotency_key TEXT UNIQUE,
                    metadata        TEXT
                )
                """
            )
            actions.append("CREATE TABLE federated_transactions")

        if not _index_exists(cur, "idx_fed_tx_idempotency"):
            cur.execute(
                "CREATE UNIQUE INDEX idx_fed_tx_idempotency "
                "ON federated_transactions(idempotency_key)"
            )
            actions.append("CREATE UNIQUE INDEX idx_fed_tx_idempotency")

        if not _index_exists(cur, "idx_fed_tx_local_user"):
            cur.execute(
                "CREATE INDEX idx_fed_tx_local_user "
                "ON federated_transactions(local_user_id)"
            )
            actions.append("CREATE INDEX idx_fed_tx_local_user")

        if not _index_exists(cur, "idx_fed_tx_status"):
            cur.execute(
                "CREATE INDEX idx_fed_tx_status "
                "ON federated_transactions(status)"
            )
            actions.append("CREATE INDEX idx_fed_tx_status")

        # ---- decision: commit or rollback -----------------------------------
        if not actions:
            print("Nothing to do — DB already migrated. (Idempotent no-op.)")
            conn.execute("ROLLBACK")
            return 0

        print("Planned actions:")
        for a in actions:
            print(f"  - {a}")
        print()

        if dry_run:
            print("Dry run — no changes written. Rolling back.")
            conn.execute("ROLLBACK")
            return 0

        conn.commit()

        # Re-snapshot after commit.
        after = _summary(cur)
        _print_summary("AFTER", after)
        print()
        print(f"OK — applied {len(actions)} action(s). Committed.")
        return 0

    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        return 1
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    dry_run = False
    db_path = DEFAULT_DB_PATH

    args = list(argv[1:])
    if "--dry-run" in args:
        dry_run = True
        args.remove("--dry-run")

    if len(args) > 1:
        print(
            "Usage: python3 002_federation_schema.py [DB_PATH] [--dry-run]",
            file=sys.stderr,
        )
        return 2
    if args:
        db_path = args[0]

    return run_migration(db_path, dry_run=dry_run)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
