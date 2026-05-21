#!/usr/bin/env python3
"""
Migration 001 — drop the `::` delimiters from every users.handle_display.

Stefano's final decision (2026-05-21): handles are now `slot:slot:slot` locally
and `slot:slot:slot@node.domain` when federated. The old `::slot:slot:slot::`
form is gone.

What this script does:
  1. Connects to the SQLite DB (path arg or default `ubi_bot.db`).
  2. Strips a leading `::` and trailing `::` from every users.handle_display.
  3. Idempotent — rows whose handle_display already lacks the wrappers are
     left untouched, so re-running the script is a no-op.
  4. Prints a before/after summary so the operator can sanity-check.
  5. Runs inside a single transaction. Any error rolls back the whole thing.

It does NOT touch handle_slot1/2/3 — those columns never carried the `::`
delimiters in the first place. Only the precomputed display string changes.

Usage:
    python3 migrations/001_drop_handle_delimiters.py              # default ubi_bot.db
    python3 migrations/001_drop_handle_delimiters.py /path/to/db  # explicit path
    python3 migrations/001_drop_handle_delimiters.py --dry-run    # report only

Exit codes:
    0  success (including the no-op idempotent case)
    1  failure (rolled back)
    2  bad arguments / DB not found
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


DEFAULT_DB_PATH = "ubi_bot.db"


def needs_migration(handle_display: str) -> bool:
    """A row needs migration if it starts with `::` AND ends with `::`."""
    return (
        isinstance(handle_display, str)
        and handle_display.startswith("::")
        and handle_display.endswith("::")
        and len(handle_display) > 4
    )


def strip_delimiters(handle_display: str) -> str:
    """`::house:cat:888::` -> `house:cat:888`."""
    return handle_display[2:-2]


def run_migration(db_path: str, dry_run: bool = False) -> int:
    p = Path(db_path)
    if not p.exists():
        print(f"ERROR: database file not found: {db_path}", file=sys.stderr)
        return 2

    print(f"Migration 001 — drop `::` from handle_display")
    print(f"Database: {db_path}")
    print(f"Dry run:  {dry_run}")
    print("-" * 60)

    conn = sqlite3.connect(db_path)
    try:
        # Use a row factory so we can read by name.
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # BEGIN is implicit on first DML; start an explicit transaction to be
        # safe across Python sqlite3 versions and isolation_level changes.
        cur.execute("BEGIN")

        cur.execute("SELECT id, handle_display FROM users ORDER BY id")
        rows = cur.fetchall()

        total = len(rows)
        to_migrate = [r for r in rows if needs_migration(r["handle_display"])]
        already_ok = total - len(to_migrate)

        print(f"Rows in users:                {total}")
        print(f"Already in new format:        {already_ok}")
        print(f"Will be stripped (`::` -> ''): {len(to_migrate)}")
        print()

        if to_migrate:
            print("Sample of changes (up to 10):")
            for r in to_migrate[:10]:
                old = r["handle_display"]
                new = strip_delimiters(old)
                print(f"  id={r['id']:<5}  {old!r}  ->  {new!r}")
            if len(to_migrate) > 10:
                print(f"  ... and {len(to_migrate) - 10} more.")
            print()

        if not to_migrate:
            print("Nothing to do — DB already migrated. (Idempotent no-op.)")
            conn.execute("ROLLBACK")
            return 0

        if dry_run:
            print("Dry run — no changes written. Rolling back.")
            conn.execute("ROLLBACK")
            return 0

        # Apply updates. Use parameterised UPDATE per row — there are at most a
        # few hundred users per node, so per-row is fine and keeps the SQL
        # legible. The single transaction guarantees atomicity.
        for r in to_migrate:
            new_value = strip_delimiters(r["handle_display"])
            cur.execute(
                "UPDATE users SET handle_display = ? WHERE id = ?",
                (new_value, r["id"]),
            )

        conn.commit()

        # Post-commit verification — re-read the table and confirm none of the
        # remaining rows still carry `::` wrappers.
        cur.execute("SELECT COUNT(*) FROM users WHERE handle_display LIKE '::%::'")
        remaining = cur.fetchone()[0]
        if remaining != 0:
            # Shouldn't be possible after our update loop, but defensive.
            print(
                f"WARNING: {remaining} row(s) still match `::%::` after migration. "
                f"Manual inspection recommended.",
                file=sys.stderr,
            )
            return 1

        print(f"OK — migrated {len(to_migrate)} row(s). Committed.")
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
        print("Usage: python3 001_drop_handle_delimiters.py [DB_PATH] [--dry-run]", file=sys.stderr)
        return 2
    if args:
        db_path = args[0]

    return run_migration(db_path, dry_run=dry_run)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
