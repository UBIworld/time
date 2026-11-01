# Changelog

Versions are tagged `vX.Y.Z` on `main`. Each tag has a GitHub Release with the same notes.
Staging (`@timeubi_stagingbot`, app.ubi.world) runs the branch under review; `main` is what has been approved.

## Unreleased (on staging, awaiting approval)

- **Today / Vault switch** at the top of the wallet. Today shows the 24-hour ring; Vault shows the Vault ring with held, locked-in-coupons and room left. Each has a one-line note and a link to the spec ("How a day works", "How the Vault works").
- **Action buttons** Give, Receive and Contacts under the ring; Give time now starts on the first screen.
- The two-line day card is replaced by one compact line with the countdown.
- **Numbers in a clear sans font.** The serif font's old-style zero looked like the letter o; amounts and the countdown now use lining figures.
- **Wallet text.** Ring centre shows hours and minutes with room around it; a "A new 24 hours arrive in" card replaces the long sentence; the Vault note says it never expires.
- **Flag-only top bar, and links** to the book, the spec and the ledger at the bottom of the wallet and coupon pages (opened in Telegram's browser inside the Mini App).
- **Contacts.** People you exchange time with become contacts automatically, on both sides. You can add someone by hand with their handle, or by Telegram name if they switched on "Let people find me by my Telegram name" (off by default; the lookup shows only their UBI handle). Tap Give on a contact to make a coupon only they can open.
- **Fix:** new versions of the app now show up at once instead of waiting up to four hours behind Cloudflare's cache.
- **Time rings.** The top of the wallet is now a 24-hour ring: left to give (sky), pending in open coupons (orange), given today (green), with the numbers beside it. A smaller ring shows the Time Vault against its capacity, plus time locked in open coupons.
- **Copy handle.** The handle pill at the top has a copy icon; one tap copies the handle and the icon turns into a check.

## v0.1.0 · 2026-11-01

First version with time coupons and a visual app.

- **Time coupons.** `/coupon` makes a signed QR coupon. The time leaves the Wallet at issuance, whoever opens it first receives it, and an unopened coupon flows to the Universal Circles at expiry. `/coupons`, `/redeem`, `/start c_<id>`.
- **Web wallet and Telegram Mini App** at app.ubi.world: balance, make a coupon, receive, coupon list, history. Login by one-time code, or automatically inside Telegram.
- **Removed** the Blue/Red feedback signal and `/send`. There is one kind of time, and sending is by coupon.
- **Security review fixes:** race-safe issuance and redemption, public coupon ids that never expose the bearer id, rate-limited login codes, CSP and origin checks.
- Staging environment: separate bot, database and node keys, run by systemd.
