"""
UBI Bot — Database Layer
SQLite schema, initialization, and all async DB operations.
All time values stored as integers (seconds). No floats, no rounding.
"""

import aiosqlite
from config import (
    DB_PATH,
    DAILY_WALLET_AMOUNT,
    VAULT_CAPACITY_TIER1,
    LOCAL_NODE_DOMAIN,
)
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Reserved handles — no user may register these (Universal Circle identities).
# Stored as the exact handle_display strings that would be produced by
# build_handle(slot, slot, slot) for each emoji slot.
# ---------------------------------------------------------------------------

RESERVED_HANDLES: frozenset[str] = frozenset({
    "❤️:❤️:❤️",  # ❤️  Health
    "\U0001f34e:\U0001f34e:\U0001f34e",         # 🍎  Food
    "\U0001f3e0:\U0001f3e0:\U0001f3e0",         # 🏠  Home
    "\U0001f33f:\U0001f33f:\U0001f33f",         # 🌿  Nature
    "\U0001f4d6:\U0001f4d6:\U0001f4d6",         # 📖  Learn
    # AI Cats team members — server-side identities, never available to real users
    "pedro:pedro:pedro",
    "bella:bella:bella",
    "milo:milo:milo",
    "tiramisu:tiramisu:tiramisu",
    "oscar:oscar:oscar",
    "siam:siam:siam",
    "raffaello:raffaello:raffaello",
})


async def init_db():
    """Create tables if they don't exist. Called once at bot startup."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")  # safe concurrent reads
        await db.execute("PRAGMA foreign_keys=ON")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id     INTEGER UNIQUE NOT NULL,
                username        TEXT,
                handle_slot1    TEXT NOT NULL,
                handle_slot2    TEXT NOT NULL,
                handle_slot3    TEXT NOT NULL,
                handle_display  TEXT UNIQUE NOT NULL,
                daily_wallet    INTEGER NOT NULL DEFAULT 86400,
                time_vault      INTEGER NOT NULL DEFAULT 0,
                vault_tier      INTEGER NOT NULL DEFAULT 1,
                vault_capacity  INTEGER NOT NULL DEFAULT 86400,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                last_reset_at   TEXT NOT NULL DEFAULT (datetime('now')),
                node_domain     TEXT,
                is_local        INTEGER NOT NULL DEFAULT 1
            )
        """)

        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_handle_slots
            ON users(handle_slot1, handle_slot2, handle_slot3)
        """)

        # -----------------------------------------------------------------------
        # Federation schema (stage 1, architecture-agnostic)
        #
        # If this DB pre-dates federation, the users table won't have the
        # `node_domain` / `is_local` columns from the CREATE TABLE above (the
        # IF NOT EXISTS skipped it). Add them with ALTER TABLE — idempotent
        # because we check PRAGMA table_info first.
        #
        # The authoritative migration of *existing rows* (populating
        # node_domain for already-registered users) lives in
        # migrations/002_federation_schema.py. This block just makes sure
        # the columns exist so newly-created bots / fresh installs work.
        # -----------------------------------------------------------------------
        cursor = await db.execute("PRAGMA table_info(users)")
        user_cols = {row[1] for row in await cursor.fetchall()}
        if "node_domain" not in user_cols:
            await db.execute("ALTER TABLE users ADD COLUMN node_domain TEXT")
        if "is_local" not in user_cols:
            await db.execute(
                "ALTER TABLE users ADD COLUMN is_local INTEGER NOT NULL DEFAULT 1"
            )

        # Backfill node_domain for any user row where it's still NULL — this
        # keeps the column populated for fresh installs and any rows that
        # slipped past 002. Uses the configured LOCAL_NODE_DOMAIN.
        await db.execute(
            "UPDATE users SET node_domain = ? WHERE node_domain IS NULL",
            (LOCAL_NODE_DOMAIN,),
        )

        # Known federation peers. `public_key` and `metadata` are kept nullable
        # / open so we can pin Ed25519 keys (HTTP+JSON transport) or Avalanche
        # subnet/chain identifiers without a schema change once Stefano picks
        # the transport layer.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS peer_nodes (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                domain          TEXT UNIQUE NOT NULL,
                discovered_at   INTEGER NOT NULL,
                last_seen_at    INTEGER,
                status          TEXT NOT NULL DEFAULT 'active'
                                    CHECK (status IN ('active', 'defederated', 'unknown')),
                public_key      TEXT,
                metadata        TEXT
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_peer_nodes_domain ON peer_nodes(domain)"
        )

        # Cross-node transfers. Kept distinct from the local `transactions`
        # table because federated transfers may be pending/reverted/failed
        # whereas local transfers are atomic and final the moment they hit
        # the DB.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS federated_transactions (
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
        """)
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_fed_tx_idempotency "
            "ON federated_transactions(idempotency_key)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fed_tx_local_user "
            "ON federated_transactions(local_user_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fed_tx_status "
            "ON federated_transactions(status)"
        )

        await db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id       INTEGER NOT NULL REFERENCES users(id),
                recipient_id    INTEGER NOT NULL REFERENCES users(id),
                amount          INTEGER NOT NULL,
                wallet_part     INTEGER NOT NULL DEFAULT 0,
                vault_part      INTEGER NOT NULL DEFAULT 0,
                blue_pct        INTEGER NOT NULL DEFAULT 100,
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_resets (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL REFERENCES users(id),
                wallet_remaining INTEGER NOT NULL,
                flowed_to_pool  INTEGER NOT NULL,
                reset_at        TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

        # Universal Circles pool — one row per circle.
        # circle_key is the canonical identifier; display_name is for UI.
        # The legacy single-pool row is preserved as circle_key='legacy' so
        # no historical seconds are lost, but all new flows go to the 5 circles.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS universal_circles_pool (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                circle_key      TEXT UNIQUE NOT NULL,
                display_name    TEXT NOT NULL,
                total_seconds   INTEGER NOT NULL DEFAULT 0,
                last_updated    TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

        # Migrate: if the old single-row schema (id CHECK id=1, no circle_key)
        # is still in place we need to rebuild the table around the new schema.
        # The CREATE TABLE above won't fire if the table already exists, so we
        # inspect the columns and handle the two cases explicitly.
        cursor = await db.execute("PRAGMA table_info(universal_circles_pool)")
        col_names = [row[1] for row in await cursor.fetchall()]
        if "circle_key" not in col_names:
            # Old single-row schema detected — grab existing balance, rebuild.
            cursor = await db.execute(
                "SELECT total_seconds FROM universal_circles_pool WHERE id = 1"
            )
            row = await cursor.fetchone()
            legacy_seconds = row[0] if row else 0

            await db.execute("DROP TABLE universal_circles_pool")
            await db.execute("""
                CREATE TABLE universal_circles_pool (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    circle_key      TEXT UNIQUE NOT NULL,
                    display_name    TEXT NOT NULL,
                    total_seconds   INTEGER NOT NULL DEFAULT 0,
                    last_updated    TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            # Preserve the legacy balance under its own row so nothing is lost.
            await db.execute(
                "INSERT INTO universal_circles_pool (circle_key, display_name, total_seconds) "
                "VALUES ('legacy', 'Legacy', ?)",
                (legacy_seconds,),
            )

        # Seed the five active circles (idempotent).
        for key, name in [
            ("health", "Health"),
            ("food",   "Food"),
            ("home",   "Home"),
            ("nature", "Nature"),
            ("learn",  "Learn"),
        ]:
            await db.execute(
                "INSERT OR IGNORE INTO universal_circles_pool (circle_key, display_name, total_seconds) "
                "VALUES (?, ?, 0)",
                (key, name),
            )

        # -----------------------------------------------------------------------
        # Community Circles — user-created shared time pools
        # -----------------------------------------------------------------------

        await db.execute("""
            CREATE TABLE IF NOT EXISTS community_circles (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                creator_id  INTEGER NOT NULL REFERENCES users(id),
                balance     INTEGER NOT NULL DEFAULT 0,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                dissolved   INTEGER NOT NULL DEFAULT 0
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS circle_members (
                circle_id   INTEGER NOT NULL REFERENCES community_circles(id),
                user_id     INTEGER NOT NULL REFERENCES users(id),
                role        TEXT NOT NULL DEFAULT 'member',
                joined_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (circle_id, user_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS circle_invites (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                circle_id       INTEGER NOT NULL REFERENCES community_circles(id),
                invitee_user_id INTEGER NOT NULL REFERENCES users(id),
                invited_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status          TEXT NOT NULL DEFAULT 'pending'
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS circle_transactions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                circle_id   INTEGER NOT NULL REFERENCES community_circles(id),
                sender_id   INTEGER NOT NULL REFERENCES users(id),
                amount      INTEGER NOT NULL,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.commit()


# ---------------------------------------------------------------------------
# User operations
# ---------------------------------------------------------------------------

async def get_user(telegram_id: int) -> dict | None:
    """Fetch a user by Telegram ID. Returns dict or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)


async def get_user_by_username(username: str) -> dict | None:
    """Fetch a user by Telegram @username (case-insensitive)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)


async def get_user_by_handle(handle_display: str) -> dict | None:
    """Fetch a user by their full handle display string (case-insensitive)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM users WHERE LOWER(handle_display) = LOWER(?)", (handle_display,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)


async def create_user(
    telegram_id: int,
    username: str | None,
    slot1: str,
    slot2: str,
    slot3: str,
) -> dict:
    """Register a new user. Returns the new user dict.

    New rows are always created with the local node's domain and
    `is_local = 1`. Remote (cached) user rows are inserted by federation
    code via a different code path once the transport layer is built.
    """
    handle_display = f"{slot1}:{slot2}:{slot3}"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (telegram_id, username, handle_slot1, handle_slot2,
                               handle_slot3, handle_display, daily_wallet, time_vault,
                               vault_tier, vault_capacity, node_domain, is_local)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?, 1)
            """,
            (
                telegram_id,
                username,
                slot1,
                slot2,
                slot3,
                handle_display,
                DAILY_WALLET_AMOUNT,
                VAULT_CAPACITY_TIER1,
                LOCAL_NODE_DOMAIN,
            ),
        )
        await db.commit()
    return await get_user(telegram_id)


async def handle_exists(slot1: str, slot2: str, slot3: str) -> bool:
    """Check if a handle combination is already taken."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM users WHERE handle_slot1 = ? AND handle_slot2 = ? AND handle_slot3 = ?",
            (slot1, slot2, slot3),
        )
        return await cursor.fetchone() is not None


# ---------------------------------------------------------------------------
# Transfer operations
# ---------------------------------------------------------------------------

async def execute_transfer(
    sender_telegram_id: int,
    recipient_telegram_id: int,
    amount: int,
    blue_pct: int = 100,
) -> dict:
    """
    Execute a time transfer from sender to recipient.
    Deducts from wallet first, then vault. Credits recipient's vault.
    Returns a result dict with details of what happened.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Fetch both users inside the same connection for consistency
        cursor = await db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (sender_telegram_id,)
        )
        sender = dict(await cursor.fetchone())

        cursor = await db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (recipient_telegram_id,)
        )
        recipient = dict(await cursor.fetchone())

        wallet_balance = sender["daily_wallet"]
        vault_balance = sender["time_vault"]
        total_available = wallet_balance + vault_balance

        if amount > total_available:
            return {"success": False, "error": "insufficient_balance", "available": total_available}

        # Calculate split: wallet first, then vault
        wallet_part = min(amount, wallet_balance)
        vault_part = amount - wallet_part

        new_sender_wallet = wallet_balance - wallet_part
        new_sender_vault = vault_balance - vault_part

        # Credit recipient vault (cap at capacity, overflow goes to pool)
        recipient_vault = recipient["time_vault"]
        recipient_capacity = recipient["vault_capacity"]
        new_recipient_vault = recipient_vault + amount
        overflow = 0
        if new_recipient_vault > recipient_capacity:
            overflow = new_recipient_vault - recipient_capacity
            new_recipient_vault = recipient_capacity

        # Update sender
        await db.execute(
            "UPDATE users SET daily_wallet = ?, time_vault = ? WHERE telegram_id = ?",
            (new_sender_wallet, new_sender_vault, sender_telegram_id),
        )

        # Update recipient
        await db.execute(
            "UPDATE users SET time_vault = ? WHERE telegram_id = ?",
            (new_recipient_vault, recipient_telegram_id),
        )

        # If overflow, distribute equally across the 5 active circles.
        # Integer division: any remainder (up to 4 seconds) goes to health.
        if overflow > 0:
            share, remainder = divmod(overflow, 5)
            for i, key in enumerate(("health", "food", "home", "nature", "learn")):
                circle_share = share + (remainder if i == 0 else 0)
                await db.execute(
                    "UPDATE universal_circles_pool "
                    "SET total_seconds = total_seconds + ?, last_updated = datetime('now') "
                    "WHERE circle_key = ?",
                    (circle_share, key),
                )

        # Record transaction
        await db.execute(
            """
            INSERT INTO transactions (sender_id, recipient_id, amount, wallet_part, vault_part, blue_pct)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (sender["id"], recipient["id"], amount, wallet_part, vault_part, blue_pct),
        )

        await db.commit()

    return {
        "success": True,
        "amount": amount,
        "wallet_part": wallet_part,
        "vault_part": vault_part,
        "blue_pct": blue_pct,
        "red_pct": 100 - blue_pct,
        "sender_wallet_remaining": new_sender_wallet,
        "sender_vault_remaining": new_sender_vault,
        "recipient_vault_new": new_recipient_vault,
        "overflow": overflow,
    }


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

async def get_transaction_history(telegram_id: int, limit: int = 10) -> list[dict]:
    """Get recent transactions for a user (as sender or recipient)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # First get user id
        cursor = await db.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return []
        user_id = row["id"]

        # node_domain is also selected so callers can render the counterparty
        # with @domain when it's a remote user (Stage 1 federation groundwork).
        cursor = await db.execute(
            """
            SELECT t.*,
                   s.handle_display as sender_handle,
                   s.telegram_id    as sender_tg_id,
                   s.node_domain    as sender_node_domain,
                   r.handle_display as recipient_handle,
                   r.telegram_id    as recipient_tg_id,
                   r.node_domain    as recipient_node_domain
            FROM transactions t
            JOIN users s ON t.sender_id = s.id
            JOIN users r ON t.recipient_id = r.id
            WHERE t.sender_id = ? OR t.recipient_id = ?
            ORDER BY t.created_at DESC
            LIMIT ?
            """,
            (user_id, user_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Daily reset
# ---------------------------------------------------------------------------

async def get_all_users() -> list[dict]:
    """Fetch all registered users."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def perform_daily_reset() -> list[dict]:
    """
    Reset all users' daily wallets. Sweep unspent wallet to Universal Circles pool.
    Returns list of reset summaries for logging/notification.
    """
    results = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users")
        users = [dict(r) for r in await cursor.fetchall()]

        total_swept = 0
        for user in users:
            remaining = user["daily_wallet"]
            total_swept += remaining

            # Log the reset
            await db.execute(
                """
                INSERT INTO daily_resets (user_id, wallet_remaining, flowed_to_pool)
                VALUES (?, ?, ?)
                """,
                (user["id"], remaining, remaining),
            )

            # Reset wallet to 24h
            await db.execute(
                """
                UPDATE users SET daily_wallet = ?, last_reset_at = datetime('now')
                WHERE id = ?
                """,
                (DAILY_WALLET_AMOUNT, user["id"]),
            )

            results.append({
                "telegram_id": user["telegram_id"],
                "handle": user["handle_display"],
                "swept": remaining,
            })

        # Distribute all swept time equally across the 5 active circles (20% each).
        # Remainder (up to 4 seconds) goes to health — keeps the math honest.
        if total_swept > 0:
            share, remainder = divmod(total_swept, 5)
            for i, key in enumerate(("health", "food", "home", "nature", "learn")):
                circle_share = share + (remainder if i == 0 else 0)
                await db.execute(
                    "UPDATE universal_circles_pool "
                    "SET total_seconds = total_seconds + ?, last_updated = datetime('now') "
                    "WHERE circle_key = ?",
                    (circle_share, key),
                )

        await db.commit()

    return results


async def get_pool_balance() -> int:
    """Get the combined total of all 5 active Universal Circles (excludes legacy row)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COALESCE(SUM(total_seconds), 0) FROM universal_circles_pool "
            "WHERE circle_key != 'legacy'"
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


# Active circles in display order — used by /circles command and any future UI.
CIRCLE_KEYS = ("health", "food", "home", "nature", "learn")


async def get_blue_red_breakdown(telegram_id: int) -> dict | None:
    """
    Sum blue_seconds and red_seconds across all transfers received by this user.
    Returns dict with total_blue, total_red, total_seconds, blue_pct (float),
    or None if the user has no received transfers.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Resolve user id
        cursor = await db.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        user_id = row["id"]

        # Fetch all received transactions; compute blue/red per row in Python
        # so we stay in integer arithmetic (blue_pct stored as 0-100 integer).
        cursor = await db.execute(
            """
            SELECT amount, blue_pct
            FROM transactions
            WHERE recipient_id = ?
            """,
            (user_id,),
        )
        rows = await cursor.fetchall()

    if not rows:
        return None

    total_blue = 0
    total_red = 0
    for r in rows:
        amount = r["amount"]
        bp = r["blue_pct"]
        # Integer math: blue_seconds = amount * blue_pct // 100,
        # red_seconds = amount - blue_seconds  (no rounding drift).
        blue = amount * bp // 100
        red = amount - blue
        total_blue += blue
        total_red += red

    total = total_blue + total_red
    blue_pct = (total_blue / total * 100) if total > 0 else 0.0

    return {
        "total_blue": total_blue,
        "total_red": total_red,
        "total_seconds": total,
        "blue_pct": blue_pct,
    }


async def get_circles_balances() -> list[dict]:
    """
    Return balance for each of the 5 active circles in canonical display order.
    Each entry: {"circle_key": str, "display_name": str, "total_seconds": int}
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # CASE expression preserves our preferred order regardless of insert order.
        cursor = await db.execute(
            """
            SELECT circle_key, display_name, total_seconds
            FROM universal_circles_pool
            WHERE circle_key IN ('health', 'food', 'home', 'nature', 'learn')
            ORDER BY CASE circle_key
                WHEN 'health' THEN 1
                WHEN 'food'   THEN 2
                WHEN 'home'   THEN 3
                WHEN 'nature' THEN 4
                WHEN 'learn'  THEN 5
            END
            """
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Community Circles — helper functions
# ---------------------------------------------------------------------------

async def create_circle(creator_telegram_id: int, name: str) -> dict:
    """
    Create a new Community Circle and add the creator as a 'creator' member.
    Returns a dict with id, name, creator_id, balance, created_at.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (creator_telegram_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            raise ValueError("Creator not found")
        creator_id = row["id"]

        cursor = await db.execute(
            "INSERT INTO community_circles (name, creator_id) VALUES (?, ?)",
            (name, creator_id),
        )
        circle_id = cursor.lastrowid

        await db.execute(
            "INSERT INTO circle_members (circle_id, user_id, role) VALUES (?, ?, 'creator')",
            (circle_id, creator_id),
        )

        await db.commit()

        cursor = await db.execute(
            "SELECT * FROM community_circles WHERE id = ?", (circle_id,)
        )
        row = await cursor.fetchone()
        return dict(row)


async def get_user_circles(telegram_id: int) -> list[dict]:
    """
    Return all active Community Circles the user belongs to.
    Each entry includes: id, name, creator_id, balance, role, member_count.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return []
        user_id = row["id"]

        cursor = await db.execute(
            """
            SELECT cc.id, cc.name, cc.creator_id, cc.balance, cm.role,
                   (SELECT COUNT(*) FROM circle_members cm2
                    WHERE cm2.circle_id = cc.id) AS member_count
            FROM community_circles cc
            JOIN circle_members cm ON cm.circle_id = cc.id AND cm.user_id = ?
            WHERE cc.dissolved = 0
            ORDER BY cc.created_at ASC
            """,
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_circle_count(telegram_id: int) -> int:
    """Total active Community Circles (created + joined) for a user."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return 0
        user_id = row[0]

        cursor = await db.execute(
            """
            SELECT COUNT(*)
            FROM circle_members cm
            JOIN community_circles cc ON cc.id = cm.circle_id
            WHERE cm.user_id = ? AND cc.dissolved = 0
            """,
            (user_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def get_circle_by_id(circle_id: int) -> dict | None:
    """Fetch a single Community Circle row by id (active or dissolved)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM community_circles WHERE id = ?", (circle_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def invite_to_circle(circle_id: int, invitee_telegram_id: int) -> dict:
    """
    Create a pending invite for invitee_telegram_id to join circle_id.
    Returns the new invite row as a dict.
    Raises ValueError if the user is already a member or has a pending invite.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (invitee_telegram_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            raise ValueError("Invitee not found")
        invitee_user_id = row["id"]

        # Already a member?
        cursor = await db.execute(
            "SELECT 1 FROM circle_members WHERE circle_id = ? AND user_id = ?",
            (circle_id, invitee_user_id),
        )
        if await cursor.fetchone():
            raise ValueError("already_member")

        # Pending invite already exists?
        cursor = await db.execute(
            "SELECT 1 FROM circle_invites WHERE circle_id = ? AND invitee_user_id = ? AND status = 'pending'",
            (circle_id, invitee_user_id),
        )
        if await cursor.fetchone():
            raise ValueError("invite_pending")

        cursor = await db.execute(
            "INSERT INTO circle_invites (circle_id, invitee_user_id) VALUES (?, ?)",
            (circle_id, invitee_user_id),
        )
        invite_id = cursor.lastrowid
        await db.commit()

        cursor = await db.execute(
            "SELECT * FROM circle_invites WHERE id = ?", (invite_id,)
        )
        row = await cursor.fetchone()
        return dict(row)


async def accept_invite(invite_id: int, user_telegram_id: int) -> bool:
    """
    Mark invite as accepted and add user to circle_members.
    Returns True on success, False if the invite was not found / not pending.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            "SELECT * FROM circle_invites WHERE id = ? AND status = 'pending'",
            (invite_id,),
        )
        invite = await cursor.fetchone()
        if invite is None:
            return False

        cursor = await db.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (user_telegram_id,)
        )
        row = await cursor.fetchone()
        if row is None or row["id"] != invite["invitee_user_id"]:
            return False  # wrong user trying to accept

        await db.execute(
            "UPDATE circle_invites SET status = 'accepted' WHERE id = ?", (invite_id,)
        )
        await db.execute(
            "INSERT OR IGNORE INTO circle_members (circle_id, user_id, role) VALUES (?, ?, 'member')",
            (invite["circle_id"], invite["invitee_user_id"]),
        )
        await db.commit()
        return True


async def ignore_invite(invite_id: int) -> bool:
    """Mark an invite as ignored. Returns True if a pending invite was found."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE circle_invites SET status = 'ignored' WHERE id = ? AND status = 'pending'",
            (invite_id,),
        )
        await db.commit()
        return cursor.rowcount > 0


async def fund_circle(
    circle_id: int, sender_telegram_id: int, amount_seconds: int
) -> dict:
    """
    Deduct amount_seconds from sender's wallet (then vault if needed),
    credit the circle's balance, and log the transaction.
    Returns a result dict similar to execute_transfer.
    Raises ValueError on insufficient_balance or not_member.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (sender_telegram_id,)
        )
        sender = await cursor.fetchone()
        if sender is None:
            raise ValueError("sender_not_found")
        sender = dict(sender)

        # Confirm sender is a member of this circle
        cursor = await db.execute(
            "SELECT 1 FROM circle_members WHERE circle_id = ? AND user_id = ?",
            (circle_id, sender["id"]),
        )
        if not await cursor.fetchone():
            raise ValueError("not_member")

        # Check sufficient balance
        wallet_balance = sender["daily_wallet"]
        vault_balance = sender["time_vault"]
        total_available = wallet_balance + vault_balance
        if amount_seconds > total_available:
            raise ValueError(f"insufficient_balance:{total_available}")

        # Wallet first, vault second
        wallet_part = min(amount_seconds, wallet_balance)
        vault_part = amount_seconds - wallet_part
        new_wallet = wallet_balance - wallet_part
        new_vault = vault_balance - vault_part

        await db.execute(
            "UPDATE users SET daily_wallet = ?, time_vault = ? WHERE id = ?",
            (new_wallet, new_vault, sender["id"]),
        )
        await db.execute(
            "UPDATE community_circles SET balance = balance + ? WHERE id = ?",
            (amount_seconds, circle_id),
        )
        await db.execute(
            "INSERT INTO circle_transactions (circle_id, sender_id, amount) VALUES (?, ?, ?)",
            (circle_id, sender["id"], amount_seconds),
        )
        await db.commit()

    return {
        "success": True,
        "amount": amount_seconds,
        "wallet_part": wallet_part,
        "vault_part": vault_part,
        "sender_wallet_remaining": new_wallet,
        "sender_vault_remaining": new_vault,
    }


async def get_invite_with_circle(invite_id: int) -> dict | None:
    """Return invite row joined with circle name, or None if not found."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT ci.*, cc.name AS circle_name
            FROM circle_invites ci
            JOIN community_circles cc ON cc.id = ci.circle_id
            WHERE ci.id = ?
            """,
            (invite_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_recent_recipients(telegram_id: int, limit: int = 3) -> list[str]:
    """
    Return up to `limit` most-recently-contacted DISTINCT recipient handle_display
    values for the given sender.  Only outbound sends are considered.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return []
        user_id = row["id"]

        # GROUP BY deduplicate; MAX(t.created_at) keeps ordering by the latest
        # send to each recipient so the list feels most-recently-used.
        cursor = await db.execute(
            """
            SELECT r.handle_display
            FROM transactions t
            JOIN users r ON r.id = t.recipient_id
            WHERE t.sender_id = ?
            GROUP BY t.recipient_id
            ORDER BY MAX(t.created_at) DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
        rows = await cursor.fetchall()
        return [row["handle_display"] for row in rows]


async def dissolve_circle(circle_id: int, creator_telegram_id: int) -> bool:
    """
    Mark a Community Circle as dissolved (dissolved=1).
    Only succeeds if the caller is the creator and the circle is active.
    Returns True on success, False if not found / not creator / already dissolved.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (creator_telegram_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return False
        user_id = row["id"]

        cursor = await db.execute(
            "SELECT creator_id, dissolved FROM community_circles WHERE id = ?",
            (circle_id,),
        )
        circle = await cursor.fetchone()
        if circle is None or circle["creator_id"] != user_id or circle["dissolved"] == 1:
            return False

        await db.execute(
            "UPDATE community_circles SET dissolved = 1 WHERE id = ?", (circle_id,)
        )
        await db.commit()
        return True
