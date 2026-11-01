"""
Time for UBI — web app (Wallet / Vault / make a coupon / redeem).

Serves the same SQLite database as the bot. Runs as its own process:

    UBI_ENV_FILE=.env.staging WEB_PORT=8161 WEB_PUBLIC_URL=https://host:44443 \
        .venv/bin/python webapp.py

Login: open the link the bot gives you with /app (magic link), or, inside
Telegram as a Mini App, the page posts its initData to /auth/webapp.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

import config
import coupons
import database as db
import webauth
from wallet import format_time, parse_time_input, time_until_midnight_utc

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("ubi-web")

WEB_DIR = Path(__file__).parent / "web"
WEB_PORT = int(os.environ.get("WEB_PORT", "8161"))
WEB_PUBLIC_URL = os.environ.get("WEB_PUBLIC_URL", f"http://127.0.0.1:{WEB_PORT}").rstrip("/")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "")
if not BOT_USERNAME:
    raise SystemExit("BOT_USERNAME is not set; coupon links would be broken")
COOKIE = "ubi_session"
COUPON_ID_RE = re.compile(r"[A-Za-z0-9_-]{16}")

# Rate limit for the login code: per client IP, sliding window.
CODE_ATTEMPTS = 10
CODE_WINDOW = 600
_code_attempts: dict[str, deque] = defaultdict(deque)


def client_ip(request: web.Request) -> str:
    # Behind the Cloudflare Tunnel only CF-Connecting-IP is trustworthy.
    return request.headers.get("CF-Connecting-IP") or (request.remote or "?")


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    q = _code_attempts[ip]
    while q and q[0] < now - CODE_WINDOW:
        q.popleft()
    if len(q) >= CODE_ATTEMPTS:
        return True
    q.append(now)
    return False


async def read_json(request: web.Request) -> dict:
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text="bad json")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="bad json")
    return body


def _valid_coupon_id(request: web.Request) -> str:
    cid = request.match_info["id"]
    if not COUPON_ID_RE.fullmatch(cid):
        raise web.HTTPNotFound()
    return cid


def coupon_link(coupon_id: str) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=c_{coupon_id}"


def coupon_page(coupon_id: str) -> str:
    return f"{WEB_PUBLIC_URL}/c/{coupon_id}"


def _qr_png(data: str) -> bytes:
    import qrcode
    img = qrcode.make(data, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _json(data, status=200):
    return web.json_response(data, status=status)


# ---------------------------------------------------------------------------
# Auth plumbing
# ---------------------------------------------------------------------------

async def current_user(request: web.Request):
    tid = webauth.read_session_cookie(request.cookies.get(COOKIE))
    if tid is None:
        return None
    return await db.get_user(tid)


def _set_session(resp: web.StreamResponse, telegram_id: int) -> None:
    secure = WEB_PUBLIC_URL.startswith("https://")
    resp.set_cookie(
        COOKIE, webauth.make_session_cookie(telegram_id),
        max_age=webauth.SESSION_TTL, httponly=True, samesite="Lax", secure=secure, path="/",
    )


async def auth_code(request: web.Request):
    ip = client_ip(request)
    if _rate_limited(ip):
        return _json({"ok": False, "error": "too_many_attempts"}, 429)
    body = await read_json(request)
    code = webauth.normalize_code(str(body.get("code", "")))
    tid = await webauth.consume_login_token(code) if len(code) == 9 else None
    if tid is None:
        await asyncio.sleep(1.0)
        return _json({"ok": False, "error": "bad_code"}, 403)
    resp = _json({"ok": True})
    _set_session(resp, tid)
    return resp


async def auth_webapp(request: web.Request):
    body = await read_json(request)
    tid = webauth.verify_webapp_init_data(body.get("initData", ""))
    if tid is None:
        return _json({"ok": False, "error": "bad_init_data"}, 401)
    resp = _json({"ok": True})
    _set_session(resp, tid)
    return resp


async def logout(request: web.Request):
    resp = web.HTTPFound("/")
    resp.del_cookie(COOKIE, path="/")
    raise resp


@web.middleware
async def security_middleware(request: web.Request, handler):
    # Cross-site POSTs: the cookie is SameSite=Lax, and on top of that any
    # POST carrying an Origin must come from our own origin.
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != _own_origin():
            raise web.HTTPForbidden(text="bad origin")
    resp = await handler(request)
    h = resp.headers
    h.setdefault("Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline' https://telegram.org; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
        "font-src 'self'; base-uri 'self'; form-action 'self'; "
        "frame-ancestors https://web.telegram.org https://*.telegram.org")
    h.setdefault("Referrer-Policy", "no-referrer")
    h.setdefault("X-Content-Type-Options", "nosniff")
    return resp


def _own_origin() -> str:
    u = urlsplit(WEB_PUBLIC_URL)
    return f"{u.scheme}://{u.netloc}"


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

async def index(request: web.Request):
    html = (WEB_DIR / "index.html").read_text()
    html = html.replace("__BOT_USERNAME__", BOT_USERNAME)
    return web.Response(text=html, content_type="text/html")


async def coupon_public_page(request: web.Request):
    """Anyone can open /c/<id>: shows the coupon and a Redeem button."""
    cid = _valid_coupon_id(request)
    html = (WEB_DIR / "coupon.html").read_text()
    html = html.replace("__BOT_USERNAME__", BOT_USERNAME).replace(
        '"__COUPON_ID__"', json.dumps(cid)
    )
    return web.Response(text=html, content_type="text/html")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _coupon_view(c: dict) -> dict:
    return {
        "id": c["id"],
        "amount": c["amount"],
        "amount_text": format_time(c["amount"]),
        "status": c["status"],
        "issuer_handle": c.get("issuer_handle"),
        "redeemer_handle": c.get("redeemer_handle"),
        "issued_at": c["issued_at"],
        "expires_at": c["expires_at"],
        "link": coupon_link(c["id"]),
        "page": coupon_page(c["id"]),
    }


async def api_me(request: web.Request):
    user = await current_user(request)
    if user is None:
        return _json({"ok": False, "error": "not_logged_in"}, 401)
    tx = await db.get_transaction_history(user["telegram_id"], 5)
    my_coupons = await coupons.list_coupons_for_issuer(user["telegram_id"], 10)
    pending = await coupons.pending_amounts(user["telegram_id"])
    day = config.DAILY_WALLET_AMOUNT
    given = max(0, day - user["daily_wallet"] - pending["wallet"])
    return _json({
        "ok": True,
        "handle": user["handle_display"],
        "wallet": user["daily_wallet"],
        "wallet_text": format_time(user["daily_wallet"]),
        "vault": user["time_vault"],
        "vault_text": format_time(user["time_vault"]),
        "vault_capacity": user["vault_capacity"],
        "day_total": day,
        "wallet_pending": pending["wallet"],
        "wallet_given": given,
        "vault_pending": pending["vault"],
        "next_reset_in": time_until_midnight_utc(),
        "transactions": [
            {
                "direction": "sent" if t["sender_tg_id"] == user["telegram_id"] else "received",
                "other": t["recipient_handle"] if t["sender_tg_id"] == user["telegram_id"] else t["sender_handle"],
                "amount": t["amount"],
                "amount_text": format_time(t["amount"]),
                "at": t["created_at"],
            }
            for t in tx
        ],
        "coupons": [_coupon_view(c) for c in my_coupons],
    })


async def api_coupon_create(request: web.Request):
    user = await current_user(request)
    if user is None:
        return _json({"ok": False, "error": "not_logged_in"}, 401)
    body = await read_json(request)
    amount = body.get("amount_seconds")
    if amount is None and body.get("amount_text"):
        amount = parse_time_input(str(body["amount_text"]))
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return _json({"ok": False, "error": "bad_amount"}, 400)
    validity = body.get("validity_seconds")
    validity = int(validity) if validity else None

    result = await coupons.issue_coupon(user["telegram_id"], amount, validity)
    if not result["success"]:
        return _json({"ok": False, "error": result["error"], "available": result.get("available")}, 400)
    c = result["coupon"]
    return _json({
        "ok": True,
        "coupon": {
            "id": c["id"],
            "amount": c["amount"],
            "amount_text": format_time(c["amount"]),
            "expires_at": c["expires_at"].strftime("%Y-%m-%d %H:%M:%S"),
            "link": coupon_link(c["id"]),
            "page": coupon_page(c["id"]),
            "qr": f"/api/coupon/{c['id']}/qr.png",
        },
        "capped_to_midnight": result["capped_to_midnight"],
        "wallet": result["issuer_wallet_remaining"],
        "vault": result["issuer_vault_remaining"],
    })


async def api_coupon_get(request: web.Request):
    c = await coupons.get_coupon(_valid_coupon_id(request))
    if c is None:
        return _json({"ok": False, "error": "not_found"}, 404)
    user = await current_user(request)
    view = _coupon_view(c)
    view["is_mine"] = bool(user and user["telegram_id"] == c["issuer_telegram_id"])
    return _json({"ok": True, "coupon": view, "logged_in": user is not None})


async def api_coupon_qr(request: web.Request):
    c = await coupons.get_coupon(_valid_coupon_id(request))
    if c is None:
        raise web.HTTPNotFound()
    # The image is a pure function of the id; let Cloudflare keep it.
    return web.Response(body=_qr_png(coupon_link(c["id"])), content_type="image/png",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


async def api_redeem(request: web.Request):
    user = await current_user(request)
    if user is None:
        return _json({"ok": False, "error": "not_logged_in"}, 401)
    body = await read_json(request)
    raw = str(body.get("code", ""))
    m = re.search(r"c_([A-Za-z0-9_-]{8,})", raw) or re.search(r"/c/([A-Za-z0-9_-]{8,})", raw)
    coupon_id = m.group(1) if m else raw.strip()
    result = await coupons.redeem_coupon(coupon_id, user["telegram_id"])
    if not result["success"]:
        return _json({"ok": False, "error": result["error"]}, 400)
    c = result["coupon"]
    return _json({
        "ok": True,
        "amount": result["amount"],
        "amount_text": format_time(result["amount"]),
        "from": c["issuer_handle"],
        "overflow": result["overflow"],
        "vault": result["redeemer_vault_new"],
        "vault_text": format_time(result["redeemer_vault_new"]),
    })


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

async def on_startup(app: web.Application):
    await db.init_db()
    await coupons.init_coupons_schema()
    await webauth.init_webauth_schema()
    logger.info("Web app on http://127.0.0.1:%d  public %s  bot @%s", WEB_PORT, WEB_PUBLIC_URL, BOT_USERNAME)


def make_app() -> web.Application:
    app = web.Application(middlewares=[security_middleware], client_max_size=16 * 1024)
    app.on_startup.append(on_startup)
    app.add_routes([
        web.get("/", index),
        web.get("/c/{id}", coupon_public_page),
        web.post("/auth/code", auth_code),
        web.post("/auth/webapp", auth_webapp),
        web.get("/logout", logout),
        web.get("/api/me", api_me),
        web.post("/api/coupon", api_coupon_create),
        web.get("/api/coupon/{id}", api_coupon_get),
        web.get("/api/coupon/{id}/qr.png", api_coupon_qr),
        web.post("/api/redeem", api_redeem),
        web.static("/static", WEB_DIR / "static"),
    ])
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="127.0.0.1", port=WEB_PORT, access_log=None)
