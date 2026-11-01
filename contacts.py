"""
Contacts: the people you have given time to or received time from, plus people you add by hand.

Privacy rules:
- A Telegram name is never shown to anyone. Lookup by @username only answers for people who
  switched "let people find me by my Telegram name" on, and it only returns their UBI handle.
- Everything else is built from real exchanges, which both sides already know about.
"""

from __future__ import annotations

from typing import Optional

import aiosqlite

from database import DB_PATH


async def init_contacts_schema() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                owner_id    INTEGER NOT NULL REFERENCES users(id),
                contact_id  INTEGER NOT NULL REFERENCES users(id),
                source      TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (owner_id, contact_id)
            )
        """)
        cols = [r[1] for r in await (await db.execute("PRAGMA table_info(users)")).fetchall()]
        if "discoverable" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN discoverable INTEGER NOT NULL DEFAULT 0")
        await db.commit()


async def link_users(user_a: int, user_b: int, source: str) -> None:
    """Make two users contacts of each other (internal user ids). Safe to call repeatedly."""
    if user_a == user_b:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "INSERT OR IGNORE INTO contacts (owner_id, contact_id, source) VALUES (?, ?, ?)",
            [(user_a, user_b, source), (user_b, user_a, source)],
        )
        await db.commit()


async def _user_id(db, telegram_id: int) -> Optional[int]:
    row = await (await db.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))).fetchone()
    return row[0] if row else None


async def list_contacts(telegram_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT c.contact_id, u.handle_display AS handle, c.source, c.created_at
            FROM contacts c
            JOIN users me ON me.id = c.owner_id
            JOIN users u ON u.id = c.contact_id
            WHERE me.telegram_id = ?
            ORDER BY u.handle_display
            """,
            (telegram_id,),
        )
        return [dict(r) for r in await cursor.fetchall()]


async def add_contact_by_handle(telegram_id: int, handle: str, source: str = "manual") -> dict:
    handle = (handle or "").strip().lower()
    async with aiosqlite.connect(DB_PATH) as db:
        me = await _user_id(db, telegram_id)
        row = await (await db.execute(
            "SELECT id, handle_display FROM users WHERE lower(handle_display) = ?", (handle,))).fetchone()
        if me is None or row is None:
            return {"success": False, "error": "not_found"}
        if row[0] == me:
            return {"success": False, "error": "self"}
        # One-way: adding someone to your own list does not put you on theirs.
        await db.execute(
            "INSERT OR IGNORE INTO contacts (owner_id, contact_id, source) VALUES (?, ?, ?)",
            (me, row[0], source),
        )
        await db.commit()
    return {"success": True, "handle": row[1]}


async def remove_contact(telegram_id: int, handle: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM contacts WHERE owner_id = (SELECT id FROM users WHERE telegram_id = ?) "
            "AND contact_id = (SELECT id FROM users WHERE lower(handle_display) = ?)",
            (telegram_id, (handle or "").strip().lower()),
        )
        await db.commit()


async def is_contact(telegram_id: int, handle: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT 1 FROM contacts c JOIN users me ON me.id = c.owner_id JOIN users u ON u.id = c.contact_id "
            "WHERE me.telegram_id = ? AND lower(u.handle_display) = ?",
            (telegram_id, (handle or "").strip().lower()))).fetchone()
    return row is not None


async def set_discoverable(telegram_id: int, on: bool) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET discoverable = ? WHERE telegram_id = ?", (1 if on else 0, telegram_id))
        await db.commit()


async def find_by_username(username: str) -> Optional[str]:
    """UBI handle of the person with this Telegram name, only if they opted in."""
    name = (username or "").strip().lstrip("@").lower()
    if not name or len(name) > 64:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT handle_display FROM users WHERE discoverable = 1 AND lower(username) = ?", (name,))).fetchone()
    return row[0] if row else None
