"""
UBI Bot — Main Entry Point
@timeubibot on Telegram

Time-based Universal Basic Income bot. First implementation of the ubi.world spec.
Built with aiogram 3.x + SQLite + APScheduler.

Run: python bot.py
"""

import asyncio
import logging
import os
import re
import sys
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import config
import database as db
from database import RESERVED_HANDLES
from wallet import (
    format_time,
    format_time_full,
    build_handle,
    time_until_midnight_utc,
    parse_time_input,
    format_federated_handle,
)


def display_handle(user: dict) -> str:
    """
    Render a user row's handle for any user-facing message.

    Local users (the only kind that currently exist) get the bare form
    ("house:cat:888") — visibly identical to today. Remote / cached users
    get the federated form ("house:cat:888@tie.ubi.asia") so the recipient
    can tell at a glance the handle isn't on this node.

    `user` is the dict returned by db.get_user / db.get_user_by_handle /
    similar. Tolerates rows that pre-date the federation columns (treats
    them as local).
    """
    return format_federated_handle(
        user["handle_display"],
        user.get("node_domain"),
        config.LOCAL_NODE_DOMAIN,
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
        [KeyboardButton(text="/balance"), KeyboardButton(text="/send")],
        [KeyboardButton(text="/history"), KeyboardButton(text="/circles")],
        [KeyboardButton(text="/blue"),    KeyboardButton(text="/handle")],
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


class Send(StatesGroup):
    # Handle collection (3 parts)
    waiting_handle_part1    = State()
    waiting_handle_part2    = State()
    waiting_handle_part3    = State()
    confirming_handle       = State()
    # Time collection (HH:MM:SS)
    waiting_hours           = State()
    waiting_minutes         = State()
    waiting_seconds         = State()
    confirming_time         = State()
    # Blue percentage
    waiting_blue            = State()
    # Final confirmation
    confirming_send         = State()


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


# ---------------------------------------------------------------------------
# Helper: inline keyboards for FSM confirmations (Fix 3)
# ---------------------------------------------------------------------------

def _handle_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Yes", callback_data="handle_yes"),
        InlineKeyboardButton(text="Restart Handle", callback_data="handle_restart"),
    ]])


def _time_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Yes", callback_data="time_yes"),
        InlineKeyboardButton(text="Restart Time", callback_data="time_restart"),
    ]])


def _send_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Confirm", callback_data="send_confirm"),
        InlineKeyboardButton(text="Restart Blue", callback_data="send_restart_blue"),
    ]])


# ---------------------------------------------------------------------------
# Shared helper: pre-fill a handle and jump straight to time-entry step
# Used by Feature 1 (Send Again), Feature 2 (Nudge), Feature 4 (Reply-to-Send)
# ---------------------------------------------------------------------------

async def _prefill_handle_and_go_to_time(
    message_or_callback_msg,
    state: FSMContext,
    handle_display: str,
    sender_tg_id: int,
) -> None:
    """
    Store handle parts in FSM data and transition to waiting_hours.
    `message_or_callback_msg` is the Message object to reply to.
    Validates that the handle still exists and is not a self-send.
    """
    recipient = await db.get_user_by_handle(handle_display)
    if not recipient:
        await message_or_callback_msg.answer(
            f"Handle {mono(handle_display)} no longer exists. "
            "Use /send to pick a new recipient.",
            parse_mode="Markdown",
        )
        await state.clear()
        return

    if recipient["telegram_id"] == sender_tg_id:
        await message_or_callback_msg.answer(
            "You cannot send time to yourself. Use /send to pick a different handle."
        )
        await state.clear()
        return

    # Split handle_display back into its three slots so the rest of the FSM
    # (cb_send_confirm, send_blue, etc.) can reconstruct the full handle normally.
    # Format is always  slot1:slot2:slot3  (post-2026-05-21 — no :: delimiters).
    # We strip an optional @domain suffix in case a federated handle ever
    # arrives here through the recent-recipient path.
    bare = handle_display.split("@", 1)[0]
    parts = bare.split(":", 2)   # exactly 3 parts

    await state.update_data(
        sender_tg_id=sender_tg_id,
        handle_part_1=parts[0],
        handle_part_2=parts[1],
        handle_part_3=parts[2],
    )
    await state.set_state(Send.waiting_hours)
    await message_or_callback_msg.answer(
        f"Sending to {mono(handle_display)}.\n\n"
        "You will be prompted to enter 3 parts of the transaction amount "
        "corresponding to HH:MM:SS\n"
        "How many Hours?",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# /start — Registration
# ---------------------------------------------------------------------------

@router.message(CommandStart(), StateFilter("*"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = await db.get_user(message.from_user.id)
    if user:
        await message.answer(
            f"Welcome back, {display_handle(user)}!\n\n"
            f"Your handle is active. Use /help to see all commands.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    await message.answer(
        "Welcome to UBI.World!\n\n"
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

    await state.clear()
    await message.answer(
        f"You're in! Welcome to the time economy.\n\n"
        f"Handle: {mono(display_handle(user))}\n"
        f"Daily Wallet: {format_time(user['daily_wallet'])}\n"
        f"Time Vault: {format_time(user['time_vault'])}\n\n"
        f"You have 24 hours to give. The clock is ticking.\n"
        f"Type /help for commands.",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


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
        f"  Handle: {mono(display_handle(user))}\n"
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
# /send — Transfer time (conversational FSM, UBI handles only)
# ---------------------------------------------------------------------------

async def _do_transfer(message: Message, sender: dict, recipient: dict, amount: int, blue_pct: int) -> bool:
    """
    Execute the transfer and send confirmations to both parties.
    Returns True on success, False on failure (error already sent to user).
    """
    # Block self-send
    if recipient["telegram_id"] == sender["telegram_id"]:
        await message.answer(
            "You cannot send time to yourself. "
            "Self-deposit is not allowed in the UBI system — "
            "your Vault only grows when others send you time."
        )
        return False

    if amount < 1:
        await message.answer("Minimum transfer is 1 second.")
        return False

    result = await db.execute_transfer(
        sender_telegram_id=sender["telegram_id"],
        recipient_telegram_id=recipient["telegram_id"],
        amount=amount,
        blue_pct=blue_pct,
    )

    if not result["success"]:
        if result["error"] == "insufficient_balance":
            await message.answer(
                f"Insufficient balance.\n"
                f"You tried to send {format_time(amount)} but only have "
                f"{format_time(result['available'])} available "
                f"(Wallet + Vault combined)."
            )
        return False

    # Build source description
    source_parts = []
    if result["wallet_part"] > 0:
        source_parts.append(f"{format_time(result['wallet_part'])} from Wallet")
    if result["vault_part"] > 0:
        source_parts.append(f"{format_time(result['vault_part'])} from Vault")
    source_desc = " + ".join(source_parts)

    # Feedback description
    blue = result["blue_pct"]
    red = result["red_pct"]
    if blue == 100:
        feedback = "🟦  Blue Time  100%"
    elif red == 100:
        feedback = "🟥  Red Time  100%"
    else:
        feedback = f"🟦  Blue Time  {blue}%  /  🟥  Red Time  {red}%"

    overflow_note = ""
    if result["overflow"] > 0:
        overflow_note = (
            f"\n(Note: {format_time(result['overflow'])} exceeded recipient's Vault capacity "
            f"and flowed to Universal Circles)"
        )

    await message.answer(
        f"📤 Sent! {format_time(amount)} to {mono(display_handle(recipient))} ({feedback})\n"
        f"Source: {source_desc}\n\n"
        f"Your remaining balance:\n"
        f"  Wallet: {format_time(result['sender_wallet_remaining'])}\n"
        f"  Vault:  {format_time(result['sender_vault_remaining'])}"
        f"{overflow_note}",
        parse_mode="Markdown",
    )

    try:
        await bot.send_message(
            chat_id=recipient["telegram_id"],
            text=(
                f"🎁 You received {format_time(amount)} from {mono(display_handle(sender))} ({feedback})\n\n"
                f"Your Time Vault: {format_time(result['recipient_vault_new'])}"
                f"{overflow_note}"
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.warning(f"Could not notify recipient {recipient['telegram_id']}: {e}")

    return True


def _is_cancel(text: str) -> bool:
    return text.strip().lower() in ("cancel", "/cancel")


# --- Step 1: /send entry point ---

def _recent_recipients_keyboard(handles: list[str]) -> InlineKeyboardMarkup:
    """Build the contextual nudge keyboard: one button per recent recipient plus
    a manual-entry escape hatch at the bottom."""
    rows = []
    for h in handles:
        rows.append([InlineKeyboardButton(
            text=h,
            callback_data=f"send_recent:{h}",
        )])
    rows.append([InlineKeyboardButton(
        text="Enter handle",
        callback_data="send_recent_manual",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("send"), StateFilter("*"))
async def cmd_send(message: Message, state: FSMContext):
    await state.clear()
    sender = await db.get_user(message.from_user.id)
    if not sender:
        await message.answer("You're not registered yet. Type /start to begin.")
        return

    await state.update_data(sender_tg_id=sender["telegram_id"])

    # Feature 2 — Contextual Nudge: show recent recipients if any exist
    recent = await db.get_recent_recipients(message.from_user.id, limit=3)
    if recent:
        await message.answer(
            "Who do you want to send time to?\n"
            "Pick a recent recipient or enter a handle manually:",
            reply_markup=_recent_recipients_keyboard(recent),
        )
        await state.set_state(Send.waiting_handle_part1)
        # State stays at waiting_handle_part1 so the normal text path still works
        # if the user ignores the keyboard and types directly.
        return

    # No history — go straight to handle entry as before
    await message.answer(
        "You will be prompted to enter 3 parts of the recipient handle "
        "(format: slot1:slot2:slot3)\n"
        "What's the 1st part of the recipient?"
    )
    await state.set_state(Send.waiting_handle_part1)


# --- Feature 2 callbacks: recent-recipient buttons on /send nudge ---

@router.callback_query(Send.waiting_handle_part1, F.data.startswith("send_recent:"))
async def cb_send_recent_pick(callback: CallbackQuery, state: FSMContext):
    """User tapped a recent-recipient button — jump straight to time entry."""
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    handle_display = callback.data[len("send_recent:"):]
    data = await state.get_data()
    sender_tg_id = data.get("sender_tg_id", callback.from_user.id)
    await _prefill_handle_and_go_to_time(callback.message, state, handle_display, sender_tg_id)


@router.callback_query(Send.waiting_handle_part1, F.data == "send_recent_manual")
async def cb_send_recent_manual(callback: CallbackQuery, state: FSMContext):
    """User tapped 'Enter handle' — fall through to the normal 3-part handle flow."""
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "You will be prompted to enter 3 parts of the recipient handle "
        "(format: slot1:slot2:slot3)\n"
        "What's the 1st part of the recipient?"
    )
    # State remains waiting_handle_part1 — text handler takes over from here


# --- Step 1 handler: handle part 1 ---

@router.message(Send.waiting_handle_part1, ~F.text.startswith("/"))
async def send_handle_part1(message: Message, state: FSMContext):
    if _is_cancel(message.text):
        await state.clear()
        await message.answer("Transaction cancelled.")
        return

    part = message.text.strip()
    await state.update_data(handle_part_1=part)
    await message.answer("What's the 2nd part of the recipient?")
    await state.set_state(Send.waiting_handle_part2)


# --- Step 2 handler: handle part 2 ---

@router.message(Send.waiting_handle_part2, ~F.text.startswith("/"))
async def send_handle_part2(message: Message, state: FSMContext):
    if _is_cancel(message.text):
        await state.clear()
        await message.answer("Transaction cancelled.")
        return

    part = message.text.strip()
    await state.update_data(handle_part_2=part)
    await message.answer("What's the 3rd part of the recipient?")
    await state.set_state(Send.waiting_handle_part3)


# --- Step 3 handler: handle part 3 — shows inline confirm keyboard ---

@router.message(Send.waiting_handle_part3, ~F.text.startswith("/"))
async def send_handle_part3(message: Message, state: FSMContext):
    if _is_cancel(message.text):
        await state.clear()
        await message.answer("Transaction cancelled.")
        return

    part = message.text.strip()
    await state.update_data(handle_part_3=part)
    data = await state.get_data()
    p1, p2, p3 = data["handle_part_1"], data["handle_part_2"], part
    handle_preview = build_handle(p1, p2, p3)
    await message.answer(
        f"Is the handle {mono(handle_preview)}?",
        parse_mode="Markdown",
        reply_markup=_handle_confirm_keyboard(),
    )
    await state.set_state(Send.confirming_handle)


# --- Step 4 callback: confirm handle via inline button ---

@router.callback_query(Send.confirming_handle, F.data == "handle_yes")
async def cb_handle_yes(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    # Validate handle exists in DB
    data = await state.get_data()
    p1, p2, p3 = data["handle_part_1"], data["handle_part_2"], data["handle_part_3"]
    recipient = await db.get_user_by_handle(build_handle(p1, p2, p3))
    if not recipient:
        await callback.message.answer(
            "Handle not found. Let's try again.\n\n"
            "You will be prompted to enter 3 parts of the recipient handle "
            "(format: slot1:slot2:slot3)\n"
            "What's the 1st part of the recipient?"
        )
        await state.update_data(handle_part_1=None, handle_part_2=None, handle_part_3=None)
        await state.set_state(Send.waiting_handle_part1)
        return

    # Check self-send early
    sender = await db.get_user(callback.from_user.id)
    if recipient["telegram_id"] == sender["telegram_id"]:
        await callback.message.answer(
            "You cannot send time to yourself. Let's try a different handle.\n\n"
            "You will be prompted to enter 3 parts of the recipient handle "
            "(format: slot1:slot2:slot3)\n"
            "What's the 1st part of the recipient?"
        )
        await state.update_data(handle_part_1=None, handle_part_2=None, handle_part_3=None)
        await state.set_state(Send.waiting_handle_part1)
        return

    # Handle confirmed — move to time collection
    await callback.message.answer(
        "You will be prompted to enter 3 parts of the transaction amount "
        "corresponding to HH:MM:SS\n"
        "How many Hours?"
    )
    await state.set_state(Send.waiting_hours)


@router.callback_query(Send.confirming_handle, F.data == "handle_restart")
async def cb_handle_restart(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.update_data(handle_part_1=None, handle_part_2=None, handle_part_3=None)
    await callback.message.answer(
        "You will be prompted to enter 3 parts of the recipient handle "
        "(format: slot1:slot2:slot3)\n"
        "What's the 1st part of the recipient?"
    )
    await state.set_state(Send.waiting_handle_part1)


# --- Step 5 handler: hours ---

@router.message(Send.waiting_hours, ~F.text.startswith("/"))
async def send_hours(message: Message, state: FSMContext):
    if _is_cancel(message.text):
        await state.clear()
        await message.answer("Transaction cancelled.")
        return

    text = message.text.strip()
    if not re.match(r'^\d+$', text) or int(text) < 0:
        await message.answer("Please enter a number (e.g. 2)")
        return

    await state.update_data(time_hours=int(text))
    await message.answer("How many Minutes?")
    await state.set_state(Send.waiting_minutes)


# --- Step 6 handler: minutes ---

@router.message(Send.waiting_minutes, ~F.text.startswith("/"))
async def send_minutes(message: Message, state: FSMContext):
    if _is_cancel(message.text):
        await state.clear()
        await message.answer("Transaction cancelled.")
        return

    text = message.text.strip()
    if not re.match(r'^\d+$', text) or not (0 <= int(text) <= 59):
        await message.answer("Please enter a number between 0 and 59 (e.g. 30)")
        return

    await state.update_data(time_minutes=int(text))
    await message.answer("How many Seconds?")
    await state.set_state(Send.waiting_seconds)


# --- Step 7 handler: seconds — shows inline time confirm keyboard ---

@router.message(Send.waiting_seconds, ~F.text.startswith("/"))
async def send_seconds(message: Message, state: FSMContext):
    if _is_cancel(message.text):
        await state.clear()
        await message.answer("Transaction cancelled.")
        return

    text = message.text.strip()
    if not re.match(r'^\d+$', text) or not (0 <= int(text) <= 59):
        await message.answer("Please enter a number between 0 and 59 (e.g. 45)")
        return

    await state.update_data(time_seconds=int(text))
    data = await state.get_data()
    h, m, s = data["time_hours"], data["time_minutes"], int(text)
    await message.answer(
        f"Do you want to send {h}H {m}M {s}S?",
        reply_markup=_time_confirm_keyboard(),
    )
    await state.set_state(Send.confirming_time)


# --- Step 8 callback: confirm time via inline button ---

@router.callback_query(Send.confirming_time, F.data == "time_yes")
async def cb_time_yes(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    data = await state.get_data()
    total_seconds = data["time_hours"] * 3600 + data["time_minutes"] * 60 + data["time_seconds"]
    if total_seconds < 1:
        await callback.message.answer(
            "The total amount must be at least 1 second. Let's try again.\n"
            "How many Hours?"
        )
        await state.update_data(time_hours=None, time_minutes=None, time_seconds=None)
        await state.set_state(Send.waiting_hours)
        return

    await callback.message.answer(
        "Enter the percentage for 🟦  Blue Time  (default: 100).\n"
        "The remainder will become 🟥  Red Time  automatically.\n"
        "(Example: enter 90 → recipient gets 90% 🟦  Blue Time  and 10% 🟥  Red Time)"
    )
    await state.set_state(Send.waiting_blue)


@router.callback_query(Send.confirming_time, F.data == "time_restart")
async def cb_time_restart(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.update_data(time_hours=None, time_minutes=None, time_seconds=None)
    await callback.message.answer(
        "You will be prompted to enter 3 parts of the transaction amount "
        "corresponding to HH:MM:SS\n"
        "How many Hours?"
    )
    await state.set_state(Send.waiting_hours)


# --- Step 9 handler: blue percentage — shows inline final confirm keyboard ---

@router.message(Send.waiting_blue, ~F.text.startswith("/"))
async def send_blue(message: Message, state: FSMContext):
    if _is_cancel(message.text):
        await state.clear()
        await message.answer("Transaction cancelled.")
        return

    text = message.text.strip()

    # Empty input or explicit 100 — use default
    if text == "" or text == "100":
        blue_pct = 100
    elif re.match(r'^\d+$', text):
        val = int(text)
        if 0 <= val <= 100:
            blue_pct = val
        else:
            await message.answer(
                "Please enter a number between 0 and 100 (e.g. 90)"
            )
            return
    else:
        await message.answer(
            "Please enter a number between 0 and 100 (e.g. 90)"
        )
        return

    await state.update_data(blue_pct=blue_pct)
    data = await state.get_data()
    h = data["time_hours"]
    m = data["time_minutes"]
    s = data["time_seconds"]
    p1 = data["handle_part_1"]
    p2 = data["handle_part_2"]
    p3 = data["handle_part_3"]
    handle_preview = build_handle(p1, p2, p3)

    await message.answer(
        f"Last step. Do you want to send {h}H {m}M {s}S to {mono(handle_preview)}?",
        parse_mode="Markdown",
        reply_markup=_send_confirm_keyboard(),
    )
    await state.set_state(Send.confirming_send)


# --- Step 10 callback: final confirmation via inline button ---

@router.callback_query(Send.confirming_send, F.data == "send_confirm")
async def cb_send_confirm(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    data = await state.get_data()
    h = data["time_hours"]
    m = data["time_minutes"]
    s = data["time_seconds"]
    p1 = data["handle_part_1"]
    p2 = data["handle_part_2"]
    p3 = data["handle_part_3"]
    blue_pct = data["blue_pct"]
    total_seconds = h * 3600 + m * 60 + s

    sender = await db.get_user(callback.from_user.id)
    recipient = await db.get_user_by_handle(build_handle(p1, p2, p3))

    if not recipient:
        await state.clear()
        await callback.message.answer(
            "Handle not found. The recipient may have been removed. Transaction cancelled."
        )
        return

    success = await _do_transfer(callback.message, sender, recipient, total_seconds, blue_pct)
    await state.clear()

    if success:
        red_pct = 100 - blue_pct
        if blue_pct == 100:
            color_line = "🟦  Blue Time  100%"
        elif red_pct == 100:
            color_line = "🟥  Red Time  100%"
        else:
            color_line = f"🟦  Blue Time  {blue_pct}%  /  🟥  Red Time  {red_pct}%"
        await callback.message.answer(
            f"Sent {h}H {m}M {s}S to {mono(build_handle(p1, p2, p3))}\n"
            f"{color_line}",
            parse_mode="Markdown",
        )


@router.callback_query(Send.confirming_send, F.data == "send_restart_blue")
async def cb_send_restart_blue(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "Enter the percentage for 🟦  Blue Time  (default: 100).\n"
        "The remainder will become 🟥  Red Time  automatically.\n"
        "(Example: enter 90 → recipient gets 90% 🟦  Blue Time  and 10% 🟥  Red Time)"
    )
    await state.set_state(Send.waiting_blue)


# ---------------------------------------------------------------------------
# /history — Transaction history  (Feature 1: per-entry "Send again" buttons)
# ---------------------------------------------------------------------------

def _send_again_keyboard(recipient_handle: str) -> InlineKeyboardMarkup:
    """Inline keyboard attached to each outbound history entry."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="Send again",
            callback_data=f"history_send_again:{recipient_handle}",
        )
    ]])


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
        await message.answer("No transactions yet. Use /send to transfer time to someone!")
        return

    await message.answer(
        f"Transaction History (last {len(transactions)})\n{'=' * 35}"
    )

    for tx in transactions:
        is_sender = tx["sender_tg_id"] == message.from_user.id
        direction = "SENT" if is_sender else "RECEIVED"
        # Federation-aware: append @node_domain to the counterparty handle
        # when they're on a remote node. Local counterparties render bare.
        other_bare = tx["recipient_handle"] if is_sender else tx["sender_handle"]
        other_domain = (
            tx.get("recipient_node_domain") if is_sender
            else tx.get("sender_node_domain")
        )
        other_raw = format_federated_handle(
            other_bare, other_domain, config.LOCAL_NODE_DOMAIN
        )
        arrow = "->" if is_sender else "<-"
        blue = tx["blue_pct"]
        red = 100 - blue
        if blue == 100:
            feedback = "🟦 100%"
        elif red == 100:
            feedback = "🟥 100%"
        else:
            feedback = f"🟦{blue}%/🟥{red}%"

        entry_text = (
            f"  {direction} {format_time(tx['amount'])} {arrow} {mono(other_raw)} "
            f"({feedback}) [{tx['created_at'][:16]}]"
        )

        # Only outbound sends get the "Send again" button — inbound receipts
        # would send back to the original sender, which may not be the intent.
        if is_sender:
            await message.answer(
                entry_text,
                parse_mode="Markdown",
                reply_markup=_send_again_keyboard(other_raw),
            )
        else:
            await message.answer(entry_text, parse_mode="Markdown")


# --- Feature 1 callback: "Send again" button from history ---

@router.callback_query(F.data.startswith("history_send_again:"))
async def cb_history_send_again(callback: CallbackQuery, state: FSMContext):
    """Pre-fill the recipient from a history entry and jump to time-entry."""
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    handle_display = callback.data[len("history_send_again:"):]

    # Clear any stale FSM state before starting a fresh send flow
    await state.clear()

    sender = await db.get_user(callback.from_user.id)
    if not sender:
        await callback.message.answer("You're not registered yet. Type /start to begin.")
        return

    await _prefill_handle_and_go_to_time(
        callback.message, state, handle_display, sender["telegram_id"]
    )


# ---------------------------------------------------------------------------
# /blue — Blue / Red time breakdown across all received transfers
# ---------------------------------------------------------------------------

@router.message(Command("blue"), StateFilter("*"))
async def cmd_blue(message: Message, state: FSMContext):
    await state.clear()
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer("You're not registered yet. Type /start to begin.")
        return

    breakdown = await db.get_blue_red_breakdown(message.from_user.id)

    if breakdown is None:
        await message.answer(
            "No time received yet. Send or receive time to start building your Blue/Red history."
        )
        return

    total_blue = breakdown["total_blue"]
    total_red  = breakdown["total_red"]
    total      = breakdown["total_seconds"]
    blue_pct   = breakdown["blue_pct"]
    red_pct    = 100.0 - blue_pct

    await message.answer(
        f"Your Blue / Red Breakdown\n\n"
        f"🟦  Blue Time   {format_time_full(total_blue)} ({blue_pct:.0f}%)\n"
        f"🟥  Red Time    {format_time_full(total_red)} ({red_pct:.0f}%)\n\n"
        f"Total received  {format_time_full(total)}"
    )


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
        f"Your Handle:\n\n  {mono(display_handle(user))}\n\n"
        f"Others can send you time using this handle.",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# /help — Command reference
# ---------------------------------------------------------------------------

@router.message(Command("help"), StateFilter("*"))
async def cmd_help(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "UBI.World Bot — Command Reference\n\n"
        "/start — Register and create your Handle\n"
        "/balance — Show your Daily Wallet + Time Vault\n"
        "/send — Send time (walks you through it step by step)\n"
        "/history — Your recent transactions\n"
        "/blue — Your 🟦  Blue Time  /  🟥  Red Time  breakdown\n"
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
        "- Every transfer carries a 🟦  Blue Time  /  🟥  Red Time  satisfaction signal\n\n"
        "Time format: enter Hours, Minutes, and Seconds separately when prompted\n"
        "🟦 Blue Time: default 100%. Enter 0-100 when prompted\n"
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

    # Store the federation-aware rendering so downstream messages
    # display @domain for remote invitees once federation is live.
    await state.update_data(
        invitee_tg_id=invitee["telegram_id"],
        invitee_handle=display_handle(invitee),
    )

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
            f"Which circle do you want to invite {mono(display_handle(invitee))} to?",
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
                f"Invited by: {mono(display_handle(sender))}"
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
# Feature 4 — Reply-to-Send: reply to a bot confirmation → pre-fill /send
# ---------------------------------------------------------------------------

# Matches handles in any bot message: slot:slot:slot   (optionally @domain).
# Slot contents exclude ':', whitespace, and '@'. We anchor on a non-handle
# boundary (start-of-string or non-slot char) so things like a stray
# "24:00:00" in a time string aren't picked up — slot tokens in bot output
# are always wrapped in backticks (`mono(handle)`), so we anchor on backtick
# or whitespace boundaries to keep the match precise.
_HANDLE_RE = re.compile(
    r'(?:^|[\s`])((?:[^\s:@`]+):(?:[^\s:@`]+):(?:[^\s:@`]+)(?:@[a-zA-Z0-9.-]+)?)(?=[\s`,.!?]|$)'
)


@router.message(F.reply_to_message, StateFilter(None))
async def handle_reply_to_bot_message(message: Message, state: FSMContext):
    """
    If a user replies to a message sent by this bot that contains a UBI handle,
    extract the handle and launch the send FSM at the time-entry step —
    skipping the 3-part handle input entirely.

    Only fires when there is no active FSM state, so it never interrupts an
    ongoing /send flow.
    """
    # Confirm the replied-to message is from this bot
    replied = message.reply_to_message
    if not replied or not replied.from_user or not replied.from_user.is_bot:
        return
    if replied.from_user.id != bot.id:
        return

    # Check that the sender is registered
    sender = await db.get_user(message.from_user.id)
    if not sender:
        return  # silent ignore — they can /start separately

    # Try to find a handle in the replied-to message text
    text_to_search = replied.text or replied.caption or ""
    match = _HANDLE_RE.search(text_to_search)
    if not match:
        return

    # group(1) is the handle proper; group(0) may include a leading boundary char.
    handle_display = match.group(1)
    # Strip federated @domain suffix if present — V1 of this feature only
    # supports local recipients (federation isn't implemented yet).
    handle_display = handle_display.split("@", 1)[0]

    # Validate the handle still exists in the DB
    recipient = await db.get_user_by_handle(handle_display)
    if not recipient:
        return  # handle may have been deleted — silent ignore

    # Block self-sends before launching the FSM
    if recipient["telegram_id"] == sender["telegram_id"]:
        await message.answer("You cannot send time to yourself.")
        return

    await _prefill_handle_and_go_to_time(
        message, state, handle_display, sender["telegram_id"]
    )


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
    logger.info("Database initialized.")

    # Set up the daily reset scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        daily_reset_job,
        CronTrigger(hour=0, minute=0, second=0, timezone="UTC"),
        id="daily_reset",
        name="Daily Wallet Reset (midnight UTC)",
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
