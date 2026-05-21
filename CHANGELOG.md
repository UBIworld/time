# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **BREAKING:** Handle format simplified — removed `::` delimiters. Canonical local form is now `slot:slot:slot` (e.g. `house:cat:888`); federated form is `slot:slot:slot@node.domain` (e.g. `house:cat:888@cat.ubi.asia`). Existing nodes must run `migrations/001_drop_handle_delimiters.py` against their `ubi_bot.db` during the next deploy cycle.

### Added
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
