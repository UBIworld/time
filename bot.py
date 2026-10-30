"""
UBI Bot — Main Entry Point
@timeubibot on Telegram

Time-based Universal Basic Income bot. First implementation of the ubi.world spec.
Built with aiogram 3.x + SQLite + APScheduler.

Run: python bot.py
"""

import asyncio
import io
import logging
import os
import re
import sys
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import config
import coupons
import database as db
import webauth
from database import RESERVED_HANDLES
from wallet import (
    format_time,
    format_time_full,
    build_handle,
    time_until_midnight_utc,
    parse_time_input,
)
# Ledger: signed JSON mirror of each transaction lands on disk for
# ubi.world to render. Import is best-effort — if pynacl isn't installed
# we degrade to "no ledger" without breaking the bot.
try:
    import ledger as _ledger
except Exception as _ledger_exc:  # pragma: no cover
    _ledger = None
    logging.getLogger("ubi-bot").warning(
        "ledger module unavailable (%s); transactions will not be mirrored to ubi.world",
        _ledger_exc,
    )

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ubi-bot")

# ---------------------------------------------------------------------------
# Bot and dispatcher setup
# ---------------------------------------------------------------------------

bot = Bot(token=config.BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# ---------------------------------------------------------------------------
# Persistent reply keyboard (Fix 2)
# ---------------------------------------------------------------------------

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="/balance"), KeyboardButton(text="/coupon")],
        [KeyboardButton(text="/coupons"), KeyboardButton(text="/history")],
        [KeyboardButton(text="/circles"), KeyboardButton(text="/handle")],
        [KeyboardButton(text="/cc"),      KeyboardButton(text="/create")],
        [KeyboardButton(text="/help")],
    ],
    resize_keyboard=True,
    persistent=True,
    input_field_placeholder="Choose a command or type below...",
)

# ---------------------------------------------------------------------------
# FSM states
# ---------------------------------------------------------------------------

class Registration(StatesGroup):
    waiting_slot1 = State()
    waiting_slot2 = State()
    waiting_slot3 = State()
    confirming = State()


class CreateCircle(StatesGroup):
    waiting_for_name    = State()
    waiting_for_confirm = State()


class InviteToCircle(StatesGroup):
    waiting_for_handle       = State()
    waiting_for_circle_select = State()  # used when creator has multiple circles


class FundCircle(StatesGroup):
    waiting_for_circle_select = State()
    waiting_for_amount        = State()
    waiting_for_confirm       = State()


class DissolveCircle(StatesGroup):
    waiting_for_circle_select = State()
    waiting_for_confirm       = State()


# ---------------------------------------------------------------------------
# Helper: wrap a handle in backticks for monospace rendering (Fix 1)
# ---------------------------------------------------------------------------

def mono(handle: str) -> str:
    """Wrap a handle string in backticks for Telegram monospace rendering."""
    return f"`{handle}`"


def _is_cancel(text: str) -> bool:
    return text.strip().lower() in ("cancel", "/cancel")


# ---------------------------------------------------------------------------
# /start — Registration
# ---------------------------------------------------------------------------

@router.message(CommandStart(), StateFilter("*"))
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    user = await db.get_user(message.from_user.id)

    # Deep link from a coupon QR / share link: t.me/<bot>?start=c_<id>
    coupon_id = None
    if command.args and command.args.startswith("c_"):
        coupon_id = command.args[2:]

    if user:
        if coupon_id:
            await _redeem_and_report(message, coupon_id)
            return
        await message.answer(
            f"Welcome back, {user['handle_display']}!\n\n"
            f"Your handle is active. Use /help to see all commands.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    if coupon_id:
        c = await coupons.get_coupon(coupon_id)
        if c and c["status"] == "issued":
            await state.update_data(pending_coupon=coupon_id)
            await message.answer(
                f"Someone is giving you {format_time(c['amount'])}.\n"
                "Create your Handle and the time is yours."
            )
        else:
            await message.answer(_coupon_unavailable_text(c))

    await message.answer(
        "Welcome to Time for UBI.\n\n"
        "Let's create your Handle. Your identity in the time economy.\n"
        "Format: slot1:slot2:slot3\n\n"
        "You'll choose 3 slots — any word, number, or phrase you like.\n\n"
        "Enter your FIRST slot (e.g., house, moon, pizza):"
    )
    await state.set_state(Registration.waiting_slot1)


@router.message(Registration.waiting_slot1, ~F.text.startswith("/"))
async def reg_slot1(message: Message, state: FSMContext):
    slot = message.text.strip()
    if not slot or len(slot) > 50:
        await message.answer("Slot must be 1-50 characters. Try again:")
        return
    if ":" in slot:
        await message.answer("Slots cannot contain the : character. Try again:")
        return

    await state.update_data(slot1=slot)
    await message.answer(
        f"Slot 1: {slot}\n\n"
        f"Enter your SECOND slot:"
    )
    await state.set_state(Registration.waiting_slot2)


@router.message(Registration.waiting_slot2, ~F.text.startswith("/"))
async def reg_slot2(message: Message, state: FSMContext):
    slot = message.text.strip()
    if not slot or len(slot) > 50:
        await message.answer("Slot must be 1-50 characters. Try again:")
        return
    if ":" in slot:
        await message.answer("Slots cannot contain the : character. Try again:")
        return

    await state.update_data(slot2=slot)
    data = await state.get_data()
    await message.answer(
        f"Slot 1: {data['slot1']}\n"
        f"Slot 2: {slot}\n\n"
        f"Enter your THIRD slot:"
    )
    await state.set_state(Registration.waiting_slot3)


@router.message(Registration.waiting_slot3, ~F.text.startswith("/"))
async def reg_slot3(message: Message, state: FSMContext):
    slot = message.text.strip()
    if not slot or len(slot) > 50:
        await message.answer("Slot must be 1-50 characters. Try again:")
        return
    if ":" in slot:
        await message.answer("Slots cannot contain the : character. Try again:")
        return

    await state.update_data(slot3=slot)
    data = await state.get_data()

    handle_preview = build_handle(data["slot1"], data["slot2"], slot)

    # Block reserved Universal Circle handles
    if handle_preview in RESERVED_HANDLES:
        await message.answer(
            f"The handle {mono(handle_preview)} is reserved for a Universal Circle and cannot be registered.\n"
            f"Let's start over. Enter your FIRST slot:",
            parse_mode="Markdown",
        )
        await state.set_state(Registration.waiting_slot1)
        return

    # Check uniqueness
    if await db.handle_exists(data["slot1"], data["slot2"], slot):
        await message.answer(
            f"The handle {mono(handle_preview)} is already taken!\n"
            f"Let's start over. Enter your FIRST slot:",
            parse_mode="Markdown",
        )
        await state.set_state(Registration.waiting_slot1)
        return

    await message.answer(
        f"Your Handle will be:\n\n"
        f"  {mono(handle_preview)}\n\n"
        f"Type YES to confirm, or RESTART to start over.",
        parse_mode="Markdown",
    )
    await state.set_state(Registration.confirming)


@router.message(Registration.confirming, ~F.text.startswith("/"))
async def reg_confirm(message: Message, state: FSMContext):
    text = message.text.strip().upper()

    if text == "RESTART":
        await message.answer("Let's start over. Enter your FIRST slot:")
        await state.set_state(Registration.waiting_slot1)
        return

    if text != "YES":
        await message.answer("Type YES to confirm your Handle, or RESTART to start over.")
        return

    data = await state.get_data()

    # Double-check: reserved handles (defensive guard in case slot3 path was bypassed)
    handle_preview = build_handle(data["slot1"], data["slot2"], data["slot3"])
    if handle_preview in RESERVED_HANDLES:
        await message.answer(
            "That handle is reserved for a Universal Circle and cannot be registered.\n"
            "Let's start over. Enter your FIRST slot:"
        )
        await state.set_state(Registration.waiting_slot1)
        return

    # Double-check uniqueness
    if await db.handle_exists(data["slot1"], data["slot2"], data["slot3"]):
        await message.answer(
            "Someone just took that handle! Let's start over.\n"
            "Enter your FIRST slot:"
        )
        await state.set_state(Registration.waiting_slot1)
        return

    # Create the user
    username = message.from_user.username  # may be None
    user = await db.create_user(
        telegram_id=message.from_user.id,
        username=username,
        slot1=data["slot1"],
        slot2=data["slot2"],
        slot3=data["slot3"],
    )

    pending_coupon = data.get("pending_coupon")
    await state.clear()
    await message.answer(
        f"You're in! Welcome to the time economy.\n\n"
        f"Handle: {mono(user['handle_display'])}\n"
        f"Daily Wallet: {format_time(user['daily_wallet'])}\n"
        f"Time Vault: {format_time(user['time_vault'])}\n\n"
        f"You have 24 hours to give. The clock is ticking.\n"
        f"Type /help for commands.",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )
    if pending_coupon:
        await _redeem_and_report(message, pending_coupon)


# ---------------------------------------------------------------------------
# /balance — Show wallet + vault
# ---------------------------------------------------------------------------

@router.message(Command("balance"), StateFilter("*"))
async def cmd_balance(message: Message, state: FSMContext):
    await state.clear()
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer("You're not registered yet. Type /start to begin.")
        return

    countdown = time_until_midnight_utc()

    await message.answer(
        f"ℹ️ Your Balance\n"
        f"  🔄 Daily Wallet:  {format_time(user['daily_wallet'])}\n"
        f"  #️⃣ Time Vault:    {format_time(user['time_vault'])}  "
        f"(Tier {user['vault_tier']}, cap {format_time(user['vault_capacity'])})\n"
        f"  Handle: {mono(user['handle_display'])}\n"
        f"  ⏰ Next reset in: {format_time(countdown)}",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# /circles — Show Universal Circles breakdown
# ---------------------------------------------------------------------------

@router.message(Command("circles"), StateFilter("*"))
async def cmd_circles(message: Message, state: FSMContext):
    await state.clear()
    circles = await db.get_circles_balances()

    if not circles:
        await message.answer("Universal Circles data not available yet.")
        return

    # Compute formatted strings first so we know how wide the label column needs to be.
    label_width = max(len(c["display_name"]) for c in circles)
    label_width = max(label_width, len("Total"))

    lines = ["Universal Circles", ""]
    total = 0
    for c in circles:
        label = c["display_name"].ljust(label_width)
        formatted = format_time_full(c["total_seconds"])
        lines.append(f"{label}  {formatted}")
        total += c["total_seconds"]

    lines.append("")
    lines.append(f"{'Total'.ljust(label_width)}  {format_time_full(total)}")

    # Wrap in a monospace code block for aligned columns.
    body = "\n".join(lines)
    await message.answer(f"```\n{body}\n```", parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /history — Transaction history
# ---------------------------------------------------------------------------

@router.message(Command("history"), StateFilter("*"))
async def cmd_history(message: Message, state: FSMContext):
    await state.clear()
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer("You're not registered yet. Type /start to begin.")
        return

    transactions = await db.get_transaction_history(
        message.from_user.id, config.HISTORY_LIMIT
    )

    if not transactions:
        await message.answer("No transactions yet. Make a coupon with /coupon 30m")
        return

    await message.answer(
        f"Transaction History (last {len(transactions)})\n{'=' * 35}"
    )

    for tx in transactions:
        is_sender = tx["sender_tg_id"] == message.from_user.id
        direction = "SENT" if is_sender else "RECEIVED"
        other_raw = tx["recipient_handle"] if is_sender else tx["sender_handle"]
        arrow = "->" if is_sender else "<-"

        entry_text = (
            f"  {direction} {format_time(tx['amount'])} {arrow} {mono(other_raw)} "
            f"[{tx['created_at'][:16]}]"
        )
        await message.answer(entry_text, parse_mode="Markdown")


# --- Feature 1 callback: "Send again" button from history ---

# ---------------------------------------------------------------------------
# Time coupons — how time is sent (issue 02)
# ---------------------------------------------------------------------------

BOT_USERNAME: str = ""   # filled in main() from get_me()

_VALIDITY_RE = re.compile(r'\b(?:for|valid)\s+(\d+)\s*(d|day|days|h|hour|hours)\b', re.I)


def coupon_link(coupon_id: str) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=c_{coupon_id}"


def _qr_png(data: str) -> bytes:
    import qrcode
    img = qrcode.make(data, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _fmt_expiry(sql_ts: str) -> str:
    return sql_ts[:16] + " UTC"


def _coupon_unavailable_text(c: dict | None) -> str:
    if c is None:
        return "This coupon does not exist."
    if c["status"] == "redeemed":
        return "This coupon has already been redeemed."
    if c["status"] == "expired":
        return (
            f"This coupon expired at {_fmt_expiry(c['expires_at'])}. "
            "Its time flowed to the Universal Circles."
        )
    return "This coupon cannot be redeemed."


def _parse_coupon_args(text: str) -> tuple[int | None, int | None, str | None]:
    """Return (amount_seconds, validity_seconds, error)."""
    validity = None
    m = _VALIDITY_RE.search(text)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        validity = n * 86400 if unit.startswith("d") else n * 3600
        text = _VALIDITY_RE.sub("", text)
    amount = parse_time_input(text)
    if amount is None:
        return None, None, "amount"
    return amount, validity, None


@router.message(Command("send"), StateFilter("*"))
async def cmd_send_retired(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Sending time works with coupons now. Make one with /coupon, "
        "for example: /coupon 2h 30m"
    )


@router.message(Command("coupon"), StateFilter("*"))
async def cmd_coupon(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer("You're not registered yet. Type /start to begin.")
        return

    args = (command.args or "").strip()
    if not args:
        await message.answer(
            "Make a time coupon. Whoever opens it first receives the time.\n\n"
            "  /coupon 30m\n"
            "  /coupon 2h 30m\n"
            "  /coupon 1h for 3d   (only time from your Vault can outlive midnight)\n\n"
            f"Wallet: {format_time(user['daily_wallet'])}   "
            f"Vault: {format_time(user['time_vault'])}\n"
            "A coupon expires at midnight UTC unless you say otherwise. "
            "Unredeemed time flows to the Universal Circles, not back to you."
        )
        return

    amount, validity, err = _parse_coupon_args(args)
    if err:
        await message.answer("I could not read the amount. Try: /coupon 2h 30m")
        return

    result = await coupons.issue_coupon(user["telegram_id"], amount, validity)
    if not result["success"]:
        if result["error"] == "insufficient_balance":
            await message.answer(
                f"Not enough time. You asked for {format_time(amount)} and have "
                f"{format_time(result['available'])} (Wallet + Vault)."
            )
        else:
            await message.answer("Could not make the coupon.")
        return

    c = result["coupon"]
    link = coupon_link(c["id"])
    expires = c["expires_at"].strftime("%Y-%m-%d %H:%M") + " UTC"
    note = ""
    if result["capped_to_midnight"]:
        note = "\nPart of this came from your Wallet, so it expires at midnight UTC."
    caption = (
        f"Coupon for {format_time(amount)}\n"
        f"Expires {expires}{note}\n\n"
        f"Scan, or forward this message, or share the link:\n{link}\n\n"
        f"Wallet left: {format_time(result['issuer_wallet_remaining'])}   "
        f"Vault left: {format_time(result['issuer_vault_remaining'])}"
    )
    await message.answer_photo(
        photo=BufferedInputFile(_qr_png(link), filename=f"coupon-{c['id']}.png"),
        caption=caption,
    )


@router.message(Command("app"), StateFilter("*"))
async def cmd_app(message: Message, state: FSMContext):
    """Magic link into the web app (one use, ten minutes)."""
    await state.clear()
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer("You're not registered yet. Type /start to begin.")
        return
    if not config.WEB_PUBLIC_URL:
        await message.answer("The web app is not configured on this node.")
        return
    token = await webauth.create_login_token(user["telegram_id"])
    await message.answer(
        f"Your wallet in the browser (link works once, for 10 minutes):\n"
        f"{config.WEB_PUBLIC_URL}/login/{token}"
    )


@router.message(Command("coupons"), StateFilter("*"))
async def cmd_coupons(message: Message, state: FSMContext):
    await state.clear()
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer("You're not registered yet. Type /start to begin.")
        return
    rows = await coupons.list_coupons_for_issuer(user["telegram_id"], limit=10)
    if not rows:
        await message.answer("No coupons yet. Make one with /coupon 30m")
        return
    lines = ["Your coupons (last 10)", ""]
    for c in rows:
        if c["status"] == "issued":
            tail = f"open until {_fmt_expiry(c['expires_at'])}"
        elif c["status"] == "redeemed":
            tail = f"redeemed by {c['redeemer_handle']}"
        else:
            tail = "expired, went to the Circles"
        lines.append(f"{format_time(c['amount'])}  {tail}")
    await message.answer("\n".join(lines))


@router.message(Command("redeem"), StateFilter("*"))
async def cmd_redeem(message: Message, command: CommandObject, state: FSMContext):
    """Paste a coupon link or code: /redeem c_abc… or /redeem https://t.me/…?start=c_abc…"""
    await state.clear()
    raw = (command.args or "").strip()
    m = re.search(r'c_([A-Za-z0-9_-]{8,})', raw)
    if not m:
        await message.answer("Paste the coupon link or code after /redeem.")
        return
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer("You're not registered yet. Type /start to begin.")
        return
    await _redeem_and_report(message, m.group(1))


async def _redeem_and_report(message: Message, coupon_id: str) -> None:
    result = await coupons.redeem_coupon(coupon_id, message.from_user.id)
    if not result["success"]:
        err = result["error"]
        c = result.get("coupon")
        if err == "own_coupon":
            await message.answer(
                "That is your own coupon. It cannot come back to you: "
                "someone else redeems it, or at expiry it flows to the Circles."
            )
        elif err == "locked":
            await message.answer("This coupon is locked to a different Handle.")
        else:
            await message.answer(_coupon_unavailable_text(c))
        return

    c = result["coupon"]
    overflow_note = ""
    if result["overflow"] > 0:
        overflow_note = (
            f"\n({format_time(result['overflow'])} was above your Vault capacity "
            f"and flowed to the Universal Circles.)"
        )
    await message.answer(
        f"You received {format_time(result['amount'])} from {mono(c['issuer_handle'])}.\n"
        f"Your Time Vault: {format_time(result['redeemer_vault_new'])}{overflow_note}",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )
    try:
        await bot.send_message(
            chat_id=c["issuer_telegram_id"],
            text=(
                f"Your coupon for {format_time(result['amount'])} was redeemed by "
                f"{mono(c['redeemer_handle'])}."
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.warning(f"Could not notify issuer {c['issuer_telegram_id']}: {e}")


# ---------------------------------------------------------------------------
# /handle — Show your handle
# ---------------------------------------------------------------------------

@router.message(Command("handle"), StateFilter("*"))
async def cmd_handle(message: Message, state: FSMContext):
    await state.clear()
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer("You're not registered yet. Type /start to begin.")
        return

    await message.answer(
        f"Your Handle:\n\n  {mono(user['handle_display'])}\n\n"
        f"This is how others will know you.",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# /help — Command reference
# ---------------------------------------------------------------------------

@router.message(Command("help"), StateFilter("*"))
async def cmd_help(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Time for UBI bot — Command Reference\n\n"
        "/start — Register and create your Handle\n"
        "/balance — Show your Daily Wallet + Time Vault\n"
        "/coupon 2h 30m — Make a time coupon (QR + link) for someone\n"
        "/coupons — Your coupons and what happened to them\n"
        "/redeem <link> — Redeem a coupon you were given\n"
        "/app — Open your wallet in the browser\n"
        "/history — Your recent transactions\n"
        "/handle — Display your Handle\n"
        "/circles — Universal Circles balances\n\n"
        "🤗 Community Circles\n"
        "/cc — List your Community Circles + balances\n"
        "/create — Start a new Community Circle (max 4)\n"
        "/invite — Invite a user to your circle (creator only)\n"
        "/fund — Contribute time to a circle you belong to\n"
        "/dissolve — Close a circle you created\n\n"
        "/help — This message\n\n"
        "How it works:\n"
        "- Every day at midnight (UTC) you receive 24h in your Daily Wallet\n"
        "- Send time to others — it goes into their Time Vault\n"
        "- Unspent Wallet time flows to Universal Circles at midnight\n"
        "- Your Vault holds time received from others (max 24h, Tier 1)\n"
        "- Wallet is spent first, then Vault if needed\n"
        "- You send time as a coupon: the time leaves your Wallet when you make it,\n"
        "  whoever opens the coupon first gets it in their Vault\n"
        "- A coupon not redeemed by its expiry flows to the Universal Circles\n\n"
        "Handle format: word:word:word (e.g., cat:chef:888)"
    )


# ---------------------------------------------------------------------------
# Community Circles — helper keyboards
# ---------------------------------------------------------------------------

def _cc_confirm_keyboard(action: str) -> InlineKeyboardMarkup:
    """Generic Yes/Cancel inline keyboard for Community Circle confirmations."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Yes", callback_data=f"{action}_yes"),
        InlineKeyboardButton(text="Cancel", callback_data=f"{action}_cancel"),
    ]])


def _circles_select_keyboard(circles: list[dict], action: str) -> InlineKeyboardMarkup:
    """Build an inline keyboard listing circles for the user to pick one."""
    rows = []
    for c in circles:
        label = f"🤗 {c['name']}"
        rows.append([InlineKeyboardButton(
            text=label,
            callback_data=f"{action}_circle_{c['id']}",
        )])
    rows.append([InlineKeyboardButton(text="Cancel", callback_data=f"{action}_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _invite_join_keyboard(invite_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Join", callback_data=f"invite_accept_{invite_id}"),
        InlineKeyboardButton(text="Ignore", callback_data=f"invite_ignore_{invite_id}"),
    ]])


# ---------------------------------------------------------------------------
# /cc — List your Community Circles
# ---------------------------------------------------------------------------

@router.message(Command("cc"), StateFilter("*"))
async def cmd_cc(message: Message, state: FSMContext):
    await state.clear()
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer("You're not registered yet. Type /start to begin.")
        return

    circles = await db.get_user_circles(message.from_user.id)
    if not circles:
        await message.answer(
            "🤗 You are not in any Community Circles yet.\n\n"
            "Use /create to start one, or wait for an invite from another member."
        )
        return

    lines = ["🤗 Your Community Circles\n"]
    for c in circles:
        role_tag = "  (you created this)" if c["role"] == "creator" else ""
        lines.append(f"  📌 {c['name']}{role_tag}")
        lines.append(f"     Balance: {format_time_full(c['balance'])}")
        lines.append(f"     Members: {c['member_count']}")
        lines.append("")

    total = len(circles)
    lines.append(f"You are in {total} of 4 allowed circles.")

    await message.answer("\n".join(lines))


# ---------------------------------------------------------------------------
# /create — Start a new Community Circle (FSM)
# ---------------------------------------------------------------------------

@router.message(Command("create"), StateFilter("*"))
async def cmd_create(message: Message, state: FSMContext):
    await state.clear()
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer("You're not registered yet. Type /start to begin.")
        return

    count = await db.get_circle_count(message.from_user.id)
    if count >= 4:
        await message.answer(
            "🤗 You've reached the maximum of 4 Community Circles (created + joined).\n"
            "Dissolve or leave a circle before creating a new one."
        )
        return

    await message.answer(
        "🤗 Let's create your Community Circle.\n\n"
        "What would you like to name it? (any text, up to 64 characters)\n"
        "Type /cancel to abort."
    )
    await state.set_state(CreateCircle.waiting_for_name)


@router.message(CreateCircle.waiting_for_name, ~F.text.startswith("/"))
async def create_circle_name(message: Message, state: FSMContext):
    if _is_cancel(message.text):
        await state.clear()
        await message.answer("Circle creation cancelled.")
        return

    name = message.text.strip()
    if not name or len(name) > 64:
        await message.answer("Name must be 1-64 characters. Try again:")
        return

    await state.update_data(circle_name=name)
    await message.answer(
        f"Create a Community Circle named:\n\n"
        f"  🤗 {name}\n\n"
        f"Confirm?",
        reply_markup=_cc_confirm_keyboard("create_circle"),
    )
    await state.set_state(CreateCircle.waiting_for_confirm)


@router.callback_query(CreateCircle.waiting_for_confirm, F.data == "create_circle_yes")
async def cb_create_circle_yes(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    data = await state.get_data()
    name = data["circle_name"]
    await state.clear()

    # Re-check limit (idempotency guard)
    count = await db.get_circle_count(callback.from_user.id)
    if count >= 4:
        await callback.message.answer(
            "You've hit the 4-circle limit before this could be created. "
            "Dissolve a circle first."
        )
        return

    circle = await db.create_circle(callback.from_user.id, name)
    await callback.message.answer(
        f"🤗 Done! Your Community Circle \"{circle['name']}\" is live.\n\n"
        f"Use /invite to bring members in, /fund to contribute time, "
        f"or /cc to see all your circles."
    )


@router.callback_query(CreateCircle.waiting_for_confirm, F.data == "create_circle_cancel")
async def cb_create_circle_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.clear()
    await callback.message.answer("Circle creation cancelled.")


# ---------------------------------------------------------------------------
# /invite — Invite a user to your circle (creator only)
# ---------------------------------------------------------------------------

@router.message(Command("invite"), StateFilter("*"))
async def cmd_invite(message: Message, state: FSMContext):
    await state.clear()
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer("You're not registered yet. Type /start to begin.")
        return

    # Get circles where the caller is creator
    circles = await db.get_user_circles(message.from_user.id)
    creator_circles = [c for c in circles if c["role"] == "creator"]

    if not creator_circles:
        await message.answer(
            "🤗 You don't own any Community Circles.\n"
            "Use /create to start one first."
        )
        return

    await state.update_data(creator_circles=[
        {"id": c["id"], "name": c["name"]} for c in creator_circles
    ])

    await message.answer(
        "Who would you like to invite? Enter their UBI handle\n"
        "(format: slot1:slot2:slot3)\n\n"
        "Type /cancel to abort."
    )
    await state.set_state(InviteToCircle.waiting_for_handle)


@router.message(InviteToCircle.waiting_for_handle, ~F.text.startswith("/"))
async def invite_handle_input(message: Message, state: FSMContext):
    if _is_cancel(message.text):
        await state.clear()
        await message.answer("Invite cancelled.")
        return

    handle_text = message.text.strip()
    invitee = await db.get_user_by_handle(handle_text)
    if not invitee:
        await message.answer(
            "Handle not found. Please enter a valid UBI handle:\n"
            "(format: slot1:slot2:slot3)"
        )
        return

    if invitee["telegram_id"] == message.from_user.id:
        await message.answer("You can't invite yourself to your own circle. Try a different handle:")
        return

    await state.update_data(invitee_tg_id=invitee["telegram_id"], invitee_handle=invitee["handle_display"])

    data = await state.get_data()
    creator_circles = data["creator_circles"]

    if len(creator_circles) == 1:
        # Only one circle — skip selection step
        circle = creator_circles[0]
        await state.update_data(selected_circle_id=circle["id"], selected_circle_name=circle["name"])
        await _send_invite(message, state)
    else:
        # Multiple circles — ask which one
        await message.answer(
            f"Which circle do you want to invite {mono(invitee['handle_display'])} to?",
            parse_mode="Markdown",
            reply_markup=_circles_select_keyboard(creator_circles, "invite"),
        )
        await state.set_state(InviteToCircle.waiting_for_circle_select)


async def _send_invite(message: Message, state: FSMContext):
    """Execute the actual invite after handle + circle are both selected."""
    data = await state.get_data()
    circle_id = data["selected_circle_id"]
    circle_name = data["selected_circle_name"]
    invitee_tg_id = data["invitee_tg_id"]
    invitee_handle = data["invitee_handle"]

    sender = await db.get_user(message.from_user.id)

    # Check invitee's circle count
    invitee_count = await db.get_circle_count(invitee_tg_id)
    if invitee_count >= 4:
        await state.clear()
        await message.answer(
            f"{mono(invitee_handle)} is already in 4 circles and cannot join more.",
            parse_mode="Markdown",
        )
        return

    try:
        invite = await db.invite_to_circle(circle_id, invitee_tg_id)
    except ValueError as e:
        await state.clear()
        err = str(e)
        if err == "already_member":
            await message.answer(f"{mono(invitee_handle)} is already a member of this circle.", parse_mode="Markdown")
        elif err == "invite_pending":
            await message.answer(f"{mono(invitee_handle)} already has a pending invite for this circle.", parse_mode="Markdown")
        else:
            await message.answer(f"Could not send invite: {err}")
        return

    await state.clear()
    await message.answer(
        f"🤗 Invite sent to {mono(invitee_handle)} for circle \"{circle_name}\".",
        parse_mode="Markdown",
    )

    # Notify the invitee
    try:
        await bot.send_message(
            chat_id=invitee_tg_id,
            text=(
                f"🤗 {circle_name} — you've been invited to join!\n"
                f"Invited by: {mono(sender['handle_display'])}"
            ),
            parse_mode="Markdown",
            reply_markup=_invite_join_keyboard(invite["id"]),
        )
    except Exception as e:
        logger.warning(f"Could not notify invitee {invitee_tg_id} of invite: {e}")


@router.callback_query(InviteToCircle.waiting_for_circle_select, F.data.startswith("invite_circle_"))
async def cb_invite_circle_select(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    circle_id = int(callback.data.split("_")[-1])
    data = await state.get_data()
    circle = next((c for c in data["creator_circles"] if c["id"] == circle_id), None)
    if circle is None:
        await state.clear()
        await callback.message.answer("Circle not found. Invite cancelled.")
        return

    await state.update_data(selected_circle_id=circle_id, selected_circle_name=circle["name"])
    await _send_invite(callback.message, state)


@router.callback_query(InviteToCircle.waiting_for_circle_select, F.data == "invite_cancel")
async def cb_invite_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.clear()
    await callback.message.answer("Invite cancelled.")


# ---------------------------------------------------------------------------
# Invite response — Join / Ignore (no FSM state required; uses callback_data)
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("invite_accept_"))
async def cb_invite_accept(callback: CallbackQuery):
    invite_id = int(callback.data.split("_")[-1])
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    # Check circle count for the acceptor
    count = await db.get_circle_count(callback.from_user.id)
    if count >= 4:
        await callback.message.answer(
            "You're already in 4 circles and can't join more.\n"
            "Dissolve or leave a circle first."
        )
        return

    # Fetch invite+circle name before accepting (accept_invite flips status to accepted)
    invite_info = await db.get_invite_with_circle(invite_id)
    circle_name = invite_info["circle_name"] if invite_info else "the circle"

    success = await db.accept_invite(invite_id, callback.from_user.id)
    if not success:
        await callback.message.answer("This invite is no longer valid (already used or expired).")
        return

    await callback.message.answer(
        f"🤗 You've joined \"{circle_name}\"!\n"
        f"Use /cc to see your circles, /fund to contribute time."
    )


@router.callback_query(F.data.startswith("invite_ignore_"))
async def cb_invite_ignore(callback: CallbackQuery):
    invite_id = int(callback.data.split("_")[-1])
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await db.ignore_invite(invite_id)
    # Silent dismiss — no notification to inviter per spec


# ---------------------------------------------------------------------------
# /fund — Contribute time to a Community Circle (FSM)
# ---------------------------------------------------------------------------

@router.message(Command("fund"), StateFilter("*"))
async def cmd_fund(message: Message, state: FSMContext):
    await state.clear()
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer("You're not registered yet. Type /start to begin.")
        return

    circles = await db.get_user_circles(message.from_user.id)
    if not circles:
        await message.answer(
            "🤗 You are not in any Community Circles.\n"
            "Use /create to start one or wait for an invite."
        )
        return

    await state.update_data(fund_circles=[
        {"id": c["id"], "name": c["name"]} for c in circles
    ])

    if len(circles) == 1:
        c = circles[0]
        await state.update_data(selected_circle_id=c["id"], selected_circle_name=c["name"])
        await message.answer(
            f"How much time do you want to contribute to \"{c['name']}\"?\n"
            f"(format: 1h 30m, 45m, 2h, etc.)\n\n"
            f"Type /cancel to abort."
        )
        await state.set_state(FundCircle.waiting_for_amount)
    else:
        await message.answer(
            "Which circle do you want to fund?",
            reply_markup=_circles_select_keyboard(circles, "fund"),
        )
        await state.set_state(FundCircle.waiting_for_circle_select)


@router.callback_query(FundCircle.waiting_for_circle_select, F.data.startswith("fund_circle_"))
async def cb_fund_circle_select(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    circle_id = int(callback.data.split("_")[-1])
    data = await state.get_data()
    circle = next((c for c in data["fund_circles"] if c["id"] == circle_id), None)
    if circle is None:
        await state.clear()
        await callback.message.answer("Circle not found. Funding cancelled.")
        return

    await state.update_data(selected_circle_id=circle_id, selected_circle_name=circle["name"])
    await callback.message.answer(
        f"How much time do you want to contribute to \"{circle['name']}\"?\n"
        f"(format: 1h 30m, 45m, 2h, etc.)\n\n"
        f"Type /cancel to abort."
    )
    await state.set_state(FundCircle.waiting_for_amount)


@router.callback_query(FundCircle.waiting_for_circle_select, F.data == "fund_cancel")
async def cb_fund_cancel_select(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.clear()
    await callback.message.answer("Funding cancelled.")


@router.message(FundCircle.waiting_for_amount, ~F.text.startswith("/"))
async def fund_amount_input(message: Message, state: FSMContext):
    if _is_cancel(message.text):
        await state.clear()
        await message.answer("Funding cancelled.")
        return

    seconds = parse_time_input(message.text)
    if seconds is None or seconds < 1:
        await message.answer(
            "Could not parse that amount. Try formats like: 1h 30m, 45m, 2h, 30s\n"
            "Minimum is 1 second."
        )
        return

    await state.update_data(fund_amount=seconds)
    data = await state.get_data()
    circle_name = data["selected_circle_name"]

    await message.answer(
        f"Contribute {format_time_full(seconds)} to \"{circle_name}\"?",
        reply_markup=_cc_confirm_keyboard("fund_circle"),
    )
    await state.set_state(FundCircle.waiting_for_confirm)


@router.callback_query(FundCircle.waiting_for_confirm, F.data == "fund_circle_yes")
async def cb_fund_circle_yes(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    data = await state.get_data()
    circle_id = data["selected_circle_id"]
    circle_name = data["selected_circle_name"]
    amount = data["fund_amount"]
    await state.clear()

    try:
        result = await db.fund_circle(circle_id, callback.from_user.id, amount)
    except ValueError as e:
        err = str(e)
        if err.startswith("insufficient_balance"):
            available = int(err.split(":")[1])
            await callback.message.answer(
                f"Insufficient balance.\n"
                f"You tried to contribute {format_time_full(amount)} but only have "
                f"{format_time_full(available)} available (Wallet + Vault combined)."
            )
        elif err == "not_member":
            await callback.message.answer("You are not a member of this circle.")
        else:
            await callback.message.answer(f"Funding failed: {err}")
        return

    source_parts = []
    if result["wallet_part"] > 0:
        source_parts.append(f"{format_time_full(result['wallet_part'])} from Wallet")
    if result["vault_part"] > 0:
        source_parts.append(f"{format_time_full(result['vault_part'])} from Vault")
    source_desc = " + ".join(source_parts)

    await callback.message.answer(
        f"🤗 Funded! {format_time_full(amount)} contributed to \"{circle_name}\".\n"
        f"Source: {source_desc}\n\n"
        f"Your remaining balance:\n"
        f"  Wallet: {format_time(result['sender_wallet_remaining'])}\n"
        f"  Vault:  {format_time(result['sender_vault_remaining'])}"
    )


@router.callback_query(FundCircle.waiting_for_confirm, F.data == "fund_circle_cancel")
async def cb_fund_circle_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.clear()
    await callback.message.answer("Funding cancelled.")


# ---------------------------------------------------------------------------
# /dissolve — Close a Community Circle (creator only, FSM)
# ---------------------------------------------------------------------------

@router.message(Command("dissolve"), StateFilter("*"))
async def cmd_dissolve(message: Message, state: FSMContext):
    await state.clear()
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer("You're not registered yet. Type /start to begin.")
        return

    circles = await db.get_user_circles(message.from_user.id)
    creator_circles = [c for c in circles if c["role"] == "creator"]

    if not creator_circles:
        await message.answer(
            "🤗 You don't own any Community Circles to dissolve.\n"
            "Only the creator can dissolve a circle."
        )
        return

    await state.update_data(dissolve_circles=[
        {"id": c["id"], "name": c["name"]} for c in creator_circles
    ])

    if len(creator_circles) == 1:
        c = creator_circles[0]
        await state.update_data(selected_circle_id=c["id"], selected_circle_name=c["name"])
        await message.answer(
            f"Are you sure you want to dissolve \"{c['name']}\"?\n\n"
            f"This will close the circle permanently. The balance stays in the pool but "
            f"no new contributions can be made.",
            reply_markup=_cc_confirm_keyboard("dissolve_circle"),
        )
        await state.set_state(DissolveCircle.waiting_for_confirm)
    else:
        await message.answer(
            "Which circle do you want to dissolve?",
            reply_markup=_circles_select_keyboard(creator_circles, "dissolve"),
        )
        await state.set_state(DissolveCircle.waiting_for_circle_select)


@router.callback_query(DissolveCircle.waiting_for_circle_select, F.data.startswith("dissolve_circle_"))
async def cb_dissolve_circle_select(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    circle_id = int(callback.data.split("_")[-1])
    data = await state.get_data()
    circle = next((c for c in data["dissolve_circles"] if c["id"] == circle_id), None)
    if circle is None:
        await state.clear()
        await callback.message.answer("Circle not found. Dissolve cancelled.")
        return

    await state.update_data(selected_circle_id=circle_id, selected_circle_name=circle["name"])
    await callback.message.answer(
        f"Are you sure you want to dissolve \"{circle['name']}\"?\n\n"
        f"This will close the circle permanently.",
        reply_markup=_cc_confirm_keyboard("dissolve_circle"),
    )
    await state.set_state(DissolveCircle.waiting_for_confirm)


@router.callback_query(DissolveCircle.waiting_for_circle_select, F.data == "dissolve_cancel")
async def cb_dissolve_cancel_select(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.clear()
    await callback.message.answer("Dissolve cancelled.")


@router.callback_query(DissolveCircle.waiting_for_confirm, F.data == "dissolve_circle_yes")
async def cb_dissolve_circle_yes(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    data = await state.get_data()
    circle_id = data["selected_circle_id"]
    circle_name = data["selected_circle_name"]
    await state.clear()

    success = await db.dissolve_circle(circle_id, callback.from_user.id)
    if not success:
        await callback.message.answer(
            "Could not dissolve the circle. It may have already been dissolved, "
            "or you are not the creator."
        )
        return

    await callback.message.answer(
        f"🤗 \"{circle_name}\" has been dissolved.\n"
        f"Use /create to start a new circle anytime."
    )


@router.callback_query(DissolveCircle.waiting_for_confirm, F.data == "dissolve_circle_cancel")
async def cb_dissolve_circle_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.clear()
    await callback.message.answer("Dissolve cancelled.")


# ---------------------------------------------------------------------------
# /reboot — Hidden admin command: in-place process restart (admin only)
# ---------------------------------------------------------------------------

@router.message(Command("reboot"), StateFilter("*"))
async def cmd_reboot(message: Message, state: FSMContext):
    await state.clear()
    # Silent ignore for anyone who isn't the admin — no error, no acknowledgement.
    if message.from_user.id != config.ADMIN_TELEGRAM_ID:
        return
    await message.answer("Restarting...")
    # os.execv replaces the current process image in-place.
    # The new process inherits the same PID, env, and open FDs — clean restart.
    os.execv(sys.executable, [sys.executable] + sys.argv)


# ---------------------------------------------------------------------------
# Catch-all for unregistered users trying commands
# ---------------------------------------------------------------------------

@router.message()
async def catch_all(message: Message, state: FSMContext):
    # If user is in FSM state, don't interfere (handled by FSM handlers)
    current_state = await state.get_state()
    if current_state is not None:
        return

    # Otherwise, gentle nudge
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer("Hi! Type /start to register, or /help for commands.")
    else:
        await message.answer("I didn't understand that. Type /help for available commands.")


# ---------------------------------------------------------------------------
# Daily reset scheduler
# ---------------------------------------------------------------------------

async def daily_reset_job():
    """Scheduled job: sweep all wallets, reset to 24h, feed Universal Circles."""
    logger.info("Running daily wallet reset...")
    try:
        # Coupons due at midnight go to the Circles before the wallet sweep.
        await coupons.expire_due_coupons()
        results = await db.perform_daily_reset()
        total_swept = sum(r["swept"] for r in results)
        logger.info(
            f"Daily reset complete. {len(results)} users reset. "
            f"{format_time(total_swept)} swept to Universal Circles."
        )

        # Notify each user
        for r in results:
            try:
                await bot.send_message(
                    chat_id=r["telegram_id"],
                    text=(
                        f"⏰ Daily Reset\n"
                        f"Unspent wallet ({format_time(r['swept'])}) "
                        f"flowed to Universal Circles.\n"
                        f"Your Daily Wallet has been refreshed to 24h 00m 00s.\n\n"
                        f"A new day begins. You have 24 hours to give."
                    ),
                )
            except Exception as e:
                logger.warning(f"Could not send reset notification to {r['telegram_id']}: {e}")

    except Exception as e:
        logger.error(f"Daily reset failed: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    # Initialize database
    await db.init_db()
    await coupons.init_coupons_schema()
    await webauth.init_webauth_schema()
    logger.info("Database initialized.")

    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username

    # Set up the daily reset scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        daily_reset_job,
        CronTrigger(hour=0, minute=0, second=0, timezone="UTC"),
        id="daily_reset",
        name="Daily Wallet Reset (midnight UTC)",
        replace_existing=True,
    )
    scheduler.add_job(
        coupons.expire_due_coupons,
        "interval",
        minutes=5,
        id="coupon_expiry",
        name="Expire due coupons to the Universal Circles",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started. Daily reset scheduled for midnight UTC.")

    # Start polling
    logger.info("Bot starting in polling mode...")
    try:
        await dp.start_polling(bot, skip_updates=True)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
