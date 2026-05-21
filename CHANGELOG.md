# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **BREAKING:** Handle format simplified — removed `::` delimiters. Canonical local form is now `slot:slot:slot` (e.g. `house:cat:888`); federated form is `slot:slot:slot@node.domain` (e.g. `house:cat:888@cat.ubi.asia`). Existing nodes must run `migrations/001_drop_handle_delimiters.py` against their `ubi_bot.db` during the next deploy cycle.

### Added
- `migrations/001_drop_handle_delimiters.py` — idempotent, transactional migration script that strips `::` delimiters from `handle_display` values in the `users` table. Supports `--dry-run`.
- Parser now accepts optional `@node.domain` suffix on handles (forward-compatible with federation; actual cross-node transfer not yet implemented).
