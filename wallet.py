"""
UBI Bot — Wallet & Vault Logic
Time formatting, parsing, and the daily reset scheduler.
"""

import re
from datetime import datetime, timezone, timedelta


# ---------------------------------------------------------------------------
# Time formatting: seconds -> human readable
# ---------------------------------------------------------------------------

def format_time(seconds: int) -> str:
    """
    Convert seconds to HHh MMm SSs format.
    Spec says: nothing rounded, full precision preserved.
    Examples: 86400 -> "24h 00m 00s", 9000 -> "2h 30m 00s", 45 -> "0h 00m 45s"
    """
    if seconds < 0:
        return "-" + format_time(abs(seconds))

    hours = seconds // 3600
    remaining = seconds % 3600
    minutes = remaining // 60
    secs = remaining % 60
    return f"{hours}h {minutes:02d}m {secs:02d}s"


def format_time_full(seconds: int) -> str:
    """
    Scale-sensitive formatter for large time values (used by /circles).
    Always shows the full remainder down to seconds — never truncates.

    Thresholds (using 30d = 1 month, 365d = 1 year):
      < 60s         → Xs
      < 3600s       → Xm Ys
      < 86400s      → Xh Ym Zs
      < 2592000s    → X days, Yh Zm Ws
      < 31536000s   → X months, Y days, Zh Wm Vs
      >= 31536000s  → X years, Y months, Z days, Wh Vm Us
    """
    if seconds < 0:
        return "-" + format_time_full(abs(seconds))

    YEAR_S  = 365 * 86400   # 31 536 000
    MONTH_S = 30  * 86400   # 2 592 000
    DAY_S   = 86400
    HOUR_S  = 3600
    MIN_S   = 60

    rem = seconds

    if seconds < MIN_S:
        return f"{rem}s"

    if seconds < HOUR_S:
        m = rem // MIN_S
        s = rem %  MIN_S
        return f"{m}m {s:02d}s"

    if seconds < DAY_S:
        h = rem // HOUR_S;  rem -= h * HOUR_S
        m = rem // MIN_S;   rem -= m * MIN_S
        s = rem
        return f"{h}h {m:02d}m {s:02d}s"

    if seconds < MONTH_S:
        d = rem // DAY_S;   rem -= d * DAY_S
        h = rem // HOUR_S;  rem -= h * HOUR_S
        m = rem // MIN_S;   rem -= m * MIN_S
        s = rem
        day_word = "day" if d == 1 else "days"
        return f"{d} {day_word}, {h}h {m:02d}m {s:02d}s"

    if seconds < YEAR_S:
        mo = rem // MONTH_S; rem -= mo * MONTH_S
        d  = rem // DAY_S;   rem -= d  * DAY_S
        h  = rem // HOUR_S;  rem -= h  * HOUR_S
        m  = rem // MIN_S;   rem -= m  * MIN_S
        s  = rem
        month_word = "month" if mo == 1 else "months"
        day_word   = "day"   if d  == 1 else "days"
        return f"{mo} {month_word}, {d} {day_word}, {h}h {m:02d}m {s:02d}s"

    # >= 1 year
    y  = rem // YEAR_S;  rem -= y  * YEAR_S
    mo = rem // MONTH_S; rem -= mo * MONTH_S
    d  = rem // DAY_S;   rem -= d  * DAY_S
    h  = rem // HOUR_S;  rem -= h  * HOUR_S
    m  = rem // MIN_S;   rem -= m  * MIN_S
    s  = rem
    year_word  = "year"  if y  == 1 else "years"
    month_word = "month" if mo == 1 else "months"
    day_word   = "day"   if d  == 1 else "days"
    return f"{y} {year_word}, {mo} {month_word}, {d} {day_word}, {h}h {m:02d}m {s:02d}s"


def format_time_short(seconds: int) -> str:
    """
    Shorter format — omit zero components for display where space is tight.
    But always show at least hours and minutes.
    """
    hours = seconds // 3600
    remaining = seconds % 3600
    minutes = remaining // 60
    secs = remaining % 60
    if secs > 0:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    return f"{hours}h {minutes:02d}m"


# ---------------------------------------------------------------------------
# Time parsing: user input -> seconds
# ---------------------------------------------------------------------------

def parse_time_input(text: str) -> int | None:
    """
    Parse user time input into seconds.
    Accepts formats like:
      "2h 30m"
      "2h 30m 15s"
      "1h"
      "30m"
      "45s"
      "2h30m"  (no space)
      "2h 30m 0s"
    Returns total seconds, or None if parsing fails.
    """
    text = text.strip().lower()
    if not text:
        return None

    hours = 0
    minutes = 0
    seconds = 0

    # Match hours
    h_match = re.search(r'(\d+)\s*h', text)
    if h_match:
        hours = int(h_match.group(1))

    # Match minutes
    m_match = re.search(r'(\d+)\s*m(?!s)', text)  # 'm' but not 'ms'
    if m_match:
        minutes = int(m_match.group(1))

    # Match seconds
    s_match = re.search(r'(\d+)\s*s', text)
    if s_match:
        seconds = int(s_match.group(1))

    total = hours * 3600 + minutes * 60 + seconds

    if total <= 0:
        return None

    return total


# ---------------------------------------------------------------------------
# Blue/Red feedback parsing
# ---------------------------------------------------------------------------

def parse_blue_pct(text: str) -> int | None:
    """
    Parse blue percentage from send command.
    Accepts: "blue:80", "blue:100", "blue:0"
    Returns integer 0-100, or None if not found / invalid.
    """
    match = re.search(r'blue\s*:\s*(\d+)', text.lower())
    if match:
        pct = int(match.group(1))
        if 0 <= pct <= 100:
            return pct
        return None  # invalid range
    return None  # not specified — caller should default to 100


# ---------------------------------------------------------------------------
# Handle format validation
# ---------------------------------------------------------------------------

def parse_handle(text: str) -> tuple[str, str, str] | None:
    """
    Parse a full handle string: slot1:slot2:slot3
    Optionally followed by @domain (federated form, returned domain is discarded
    here — callers that need it should use parse_qualified_handle).
    Returns (slot1, slot2, slot3) or None if invalid format.

    Slot contents cannot contain ':' or '@' or whitespace. Empty slots reject.
    """
    text = text.strip()
    if not text:
        return None
    # Strip optional federated suffix
    if "@" in text:
        text = text.split("@", 1)[0]
    parts = text.split(":")
    if len(parts) == 3 and all(p and ("@" not in p) and (not any(c.isspace() for c in p)) for p in parts):
        return parts[0], parts[1], parts[2]
    return None


def parse_qualified_handle(text: str) -> dict | None:
    """
    Federation-aware handle parser.

    Accepts both:
      - "house:cat:888"                  (local form, no domain)
      - "house:cat:888@cat.ubi.asia"     (federated form)

    Returns a dict on success:
        {
            "slot1": str, "slot2": str, "slot3": str,
            "domain": str | None,        # None means "caller's local node"
            "is_local": bool,            # True iff no @domain was present
            "handle_bare": str,          # "slot1:slot2:slot3"
            "handle_full": str,          # bare, with @domain appended iff present
        }
    Or None if the input doesn't parse.

    Notes:
      - `is_local` here is a *syntactic* signal — it means "the caller didn't
        specify a domain", not "the user definitely lives on this node". The
        DB lookup is still authoritative. But for routing logic in transport
        code, this distinction is what we branch on.
      - Domain validation is loose intentionally — DNS rules are looser than
        most regexes, and tightening here just creates false negatives. We
        accept anything non-empty that has no whitespace or `:`.
    """
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None

    domain: str | None = None
    if "@" in text:
        bare, _, dom = text.partition("@")
        dom = dom.strip()
        if not dom or ":" in dom or any(c.isspace() for c in dom):
            return None
        domain = dom
        text = bare

    parts = text.split(":")
    if len(parts) != 3:
        return None
    if not all(p and ("@" not in p) and (not any(c.isspace() for c in p)) for p in parts):
        return None

    slot1, slot2, slot3 = parts
    handle_bare = f"{slot1}:{slot2}:{slot3}"
    handle_full = handle_bare if domain is None else f"{handle_bare}@{domain}"
    return {
        "slot1": slot1,
        "slot2": slot2,
        "slot3": slot3,
        "domain": domain,
        "is_local": domain is None,
        "handle_bare": handle_bare,
        "handle_full": handle_full,
    }


def build_handle(slot1: str, slot2: str, slot3: str) -> str:
    """Build handle display string from three slots (local form, no domain)."""
    return f"{slot1}:{slot2}:{slot3}"


def format_federated_handle(
    handle_bare: str,
    user_node_domain: str | None,
    local_node_domain: str,
) -> str:
    """
    Render a handle for display, appending `@node_domain` only when the user
    is NOT on the local node.

    Args:
      handle_bare:        "slot1:slot2:slot3" (no domain, no delimiters)
      user_node_domain:   the user's node_domain column from the DB; may be
                          None for legacy rows that haven't been backfilled
      local_node_domain:  config.LOCAL_NODE_DOMAIN

    Returns the display string. Local users (or unknown / NULL node_domain,
    which we treat as local for back-compat) get the bare handle. Remote
    users get the suffixed form.
    """
    if not user_node_domain or user_node_domain == local_node_domain:
        return handle_bare
    return f"{handle_bare}@{user_node_domain}"


# ---------------------------------------------------------------------------
# Countdown to next reset
# ---------------------------------------------------------------------------

def time_until_midnight_utc() -> int:
    """Seconds until next UTC midnight."""
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    delta = tomorrow - now
    return int(delta.total_seconds())


# ---------------------------------------------------------------------------
# Send command parsing
# ---------------------------------------------------------------------------

def parse_send_command(text: str) -> dict | None:
    """
    Parse the /send command arguments.
    Formats:
      /send @username 2h 30m
      /send @username 2h 30m blue:80
      /send ++[a][b][c]++ 1h
      /send ++[a][b][c]++ 1h blue:50

    Returns dict with keys: recipient, amount_seconds, blue_pct
    Or None if parsing fails.
    """
    text = text.strip()

    # Remove the /send prefix if present
    if text.lower().startswith("/send"):
        text = text[5:].strip()

    if not text:
        return None

    recipient = None
    rest = text

    # Try to match @username first
    at_match = re.match(r'@(\w+)\s+(.*)', text)
    if at_match:
        recipient = "@" + at_match.group(1)
        rest = at_match.group(2)
    else:
        # Try to match handle format slot1:slot2:slot3 with optional @domain
        # (federation parsing only — federated send isn't implemented yet).
        handle_match = re.match(
            r'((?:[^:\s@]+):(?:[^:\s@]+):(?:[^:\s@]+)(?:@[a-zA-Z0-9.-]+)?)\s+(.*)',
            text,
        )
        if handle_match:
            recipient = handle_match.group(1)
            rest = handle_match.group(2)
        else:
            return None

    # Parse time amount from the rest
    # First, separate out blue:XX if present
    blue_pct = 100  # default
    blue_val = parse_blue_pct(rest)
    if blue_val is not None:
        blue_pct = blue_val
        # Remove the blue:XX from rest before parsing time
        rest = re.sub(r'blue\s*:\s*\d+', '', rest).strip()

    amount = parse_time_input(rest)
    if amount is None:
        return None

    return {
        "recipient": recipient,
        "amount_seconds": amount,
        "blue_pct": blue_pct,
    }
