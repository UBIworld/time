# Changelog

Versions are tagged `vX.Y.Z` on `main`. Each tag has a GitHub Release with the same notes.
Staging (`@timeubi_stagingbot`, app.ubi.world) runs the branch under review; `main` is what has been approved.

## v0.1.0 · 2026-11-01

First version with time coupons and a visual app.

- **Time coupons.** `/coupon` makes a signed QR coupon. The time leaves the Wallet at issuance, whoever opens it first receives it, and an unopened coupon flows to the Universal Circles at expiry. `/coupons`, `/redeem`, `/start c_<id>`.
- **Web wallet and Telegram Mini App** at app.ubi.world: balance, make a coupon, receive, coupon list, history. Login by one-time code, or automatically inside Telegram.
- **Removed** the Blue/Red feedback signal and `/send`. There is one kind of time, and sending is by coupon.
- **Security review fixes:** race-safe issuance and redemption, public coupon ids that never expose the bearer id, rate-limited login codes, CSP and origin checks.
- Staging environment: separate bot, database and node keys, run by systemd.
