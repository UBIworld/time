# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **BREAKING:** Handle format simplified — removed `::` delimiters. Canonical local form is now `slot:slot:slot` (e.g. `house:cat:888`); federated form is `slot:slot:slot@node.domain` (e.g. `house:cat:888@cat.ubi.asia`). Existing nodes must run `migrations/001_drop_handle_delimiters.py` against their `ubi_bot.db` during the next deploy cycle.

### Added
- **Federation stage 2a: Ed25519 identity + WebFinger-style discovery.**
  - Per-node Ed25519 keypair generated on first boot via `pynacl`,
    persisted to `NODE_KEY_DIR` (default `~/.ubi-bot/`) as
    `node_private_key.pem` (mode 600) and `node_public_key.pem` (mode
    644). Idempotent — existing keys are loaded, not regenerated;
    fingerprint logged at startup.
  - Embedded aiohttp HTTP server alongside aiogram's polling loop
    (same process, same event loop). Serves
    `GET /.well-known/ubi-node` returning a JSON document with
    `node_domain`, `node_public_key` (base64), `spec_version`,
    placeholder `endpoints` for stage 2b, `software`, and
    `created_at`. All other paths return 404 with JSON body.
    Bind address/port configurable via `FEDERATION_HTTP_HOST`
    (default `0.0.0.0`) and `FEDERATION_HTTP_PORT` (default `8765`).
  - Admin-only Telegram commands:
    - `/federation_identity` — prints this node's public key + domain
      in a copy-pasteable block for sharing with peer operators.
    - `/peer_add <domain>` — fetches the peer's well-known doc,
      validates it, upserts into `peer_nodes` with `status='active'`.
      Idempotent (re-running refreshes the row, never duplicates).
    - `/peer_list` — lists every row in `peer_nodes` with status and
      key fingerprint.
    - `/peer_remove <domain>` — sets `status='defederated'`.
      Preserves the row for audit history.
  - New `federation.py` module with the plain-function library
    (`load_or_create_keypair`, `build_well_known_doc`,
    `validate_well_known_doc`, `discover_peer`, `upsert_peer_node`,
    `list_peer_nodes`, `soft_remove_peer_node`) that stage 2b will
    reuse for the signed transfer protocol.
  - New config knobs: `NODE_KEY_DIR`, `FEDERATION_HTTP_PORT`,
    `FEDERATION_HTTP_HOST`, `FEDERATION_SPEC_VERSION`.
  - **No signature verification yet.** Stage 2a is plumbing only —
    two nodes can discover each other and exchange public keys; trust
    establishment / signed transfers arrive in stage 2b.
- Federation schema groundwork: `users.node_domain`, `users.is_local`,
  new `peer_nodes` table, new `federated_transactions` table. No
  federation transport code yet — schema is architecture-agnostic
  pending transport-layer decision (HTTP+JSON+Ed25519 per current
  federation-architecture.md, or alternative under evaluation).
  Includes migration script `migrations/002_federation_schema.py`.
- `wallet.parse_qualified_handle()` — federation-aware parser that returns
  a structured dict with `slot1/slot2/slot3/domain/is_local/handle_bare/
  handle_full`. Coexists with the legacy `parse_handle()` (still returns
  the 3-tuple after stripping `@domain`).
- `wallet.format_federated_handle()` and `bot.display_handle()` — render a
  user's handle with `@node_domain` suffix only when they live on a
  remote node. Local users continue to render as `slot:slot:slot`.
- `config.LOCAL_NODE_DOMAIN` — read from `LOCAL_NODE_DOMAIN` env var,
  defaults to `localhost`. Production sets this to `cat.ubi.asia` via
  the systemd EnvironmentFile.
- `migrations/001_drop_handle_delimiters.py` — idempotent, transactional migration script that strips `::` delimiters from `handle_display` values in the `users` table. Supports `--dry-run`.
- Parser now accepts optional `@node.domain` suffix on handles (forward-compatible with federation; actual cross-node transfer not yet implemented).
