"""
UBI Bot — Federation (Stage 2a: identity + discovery + peer plumbing)

This module contains everything needed for two nodes to:
  1. Generate and persist a per-node Ed25519 keypair on first boot.
  2. Publish their own identity at `/.well-known/ubi-node` as a JSON doc.
  3. Discover another node by GETting its well-known and parsing the doc.
  4. Persist peers into the `peer_nodes` table (status / public_key / metadata).

It deliberately does NOT do:
  * Any signature verification (stage 2b).
  * The `/federation/transfer` endpoint (stage 2b — placeholders only here).
  * Any actual federated transfer logic.

The whole point of stage 2a is plumbing: two nodes can find each other and
read each other's public keys. Trust establishment happens in 2b.

See `Other Cats Inbox/federation-architecture.md` sections 2 (discovery) and
4 (trust/auth) for the design rationale.

------------------------------------------------------------------------
Keypair persistence layout (default — overridable via NODE_KEY_DIR):

    ~/.ubi-bot/
        node_private_key.pem    (mode 600, this node only)
        node_public_key.pem     (mode 644, safe to share / publish)

We use PEM-style ASCII files because they're (a) trivially readable by a
human (`cat node_public_key.pem` is debuggable) and (b) every operator
recognises the format from countless SSH / TLS keys. They are NOT the
PKCS#8 PEM that OpenSSL produces — we just wrap the raw 32-byte key
material in a BEGIN/END envelope with base64. This avoids pulling in
`cryptography` for parsing OpenSSL-style structures; pynacl already
handles raw bytes natively.

------------------------------------------------------------------------
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web
from nacl.signing import SigningKey, VerifyKey

import config


logger = logging.getLogger("ubi-bot.federation")


# ---------------------------------------------------------------------------
# Keypair management
# ---------------------------------------------------------------------------

_PRIVATE_KEY_FILENAME = "node_private_key.pem"
_PUBLIC_KEY_FILENAME = "node_public_key.pem"

_PRIVATE_PEM_HEADER = "-----BEGIN UBI NODE ED25519 PRIVATE KEY-----"
_PRIVATE_PEM_FOOTER = "-----END UBI NODE ED25519 PRIVATE KEY-----"
_PUBLIC_PEM_HEADER = "-----BEGIN UBI NODE ED25519 PUBLIC KEY-----"
_PUBLIC_PEM_FOOTER = "-----END UBI NODE ED25519 PUBLIC KEY-----"


def _pem_wrap(header: str, footer: str, raw: bytes) -> str:
    """Wrap raw key bytes in a PEM-style envelope. base64 body, 64-col wrap."""
    body = base64.b64encode(raw).decode("ascii")
    wrapped = "\n".join(body[i : i + 64] for i in range(0, len(body), 64))
    return f"{header}\n{wrapped}\n{footer}\n"


def _pem_unwrap(header: str, footer: str, text: str) -> bytes:
    """Reverse of _pem_wrap. Raises ValueError if the envelope is malformed."""
    stripped = text.strip()
    if not stripped.startswith(header) or not stripped.endswith(footer):
        raise ValueError("PEM envelope header/footer missing or mismatched")
    body = stripped[len(header) : -len(footer)].strip()
    body = "".join(body.split())  # drop any whitespace inside
    try:
        return base64.b64decode(body, validate=True)
    except Exception as exc:
        raise ValueError(f"PEM body is not valid base64: {exc}") from exc


def _ensure_key_dir(key_dir: str) -> Path:
    """
    Make sure NODE_KEY_DIR exists with mode 700. Idempotent.
    Returns the resolved Path.
    """
    p = Path(key_dir).expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True)
    try:
        # Best-effort tightening — on Windows this is a no-op, which is fine.
        os.chmod(p, 0o700)
    except OSError as exc:
        logger.warning("Could not chmod 700 on %s: %s", p, exc)
    return p


def _public_key_fingerprint(public_key_bytes: bytes) -> str:
    """
    Short stable fingerprint of a public key for logging.

    Format: first 16 hex chars of the raw key, colon-separated in groups of 4.
    Example: `a1b2:c3d4:e5f6:7890`. Enough for humans to eyeball-match across
    operator chats without dumping the full 32 bytes.
    """
    hexed = public_key_bytes.hex()
    chunks = [hexed[i : i + 4] for i in range(0, 16, 4)]
    return ":".join(chunks)


def load_or_create_keypair(key_dir: Optional[str] = None) -> dict:
    """
    Load this node's Ed25519 keypair from disk, generating it on first run.

    Idempotent: if both `node_private_key.pem` and `node_public_key.pem`
    exist and parse, they are returned unchanged. Otherwise a new keypair
    is generated and written.

    File permissions on the private key are tightened to 0600. The public
    key is left at the umask default (typically 0644) because it is, by
    design, public.

    Returns a dict:
        {
            "private_key_b64": str,   # 32 bytes base64 (the seed)
            "public_key_b64":  str,   # 32 bytes base64
            "fingerprint":     str,   # short eyeball-able fingerprint
            "key_dir":         str,   # resolved directory the files live in
            "private_key_path": str,
            "public_key_path":  str,
            "generated":       bool,  # True iff we just created the pair
        }

    Raises:
        OSError on directory / file IO problems.
        ValueError if existing files exist but are malformed (operator must
        decide whether to delete them; we refuse to silently overwrite).
    """
    if key_dir is None:
        key_dir = config.NODE_KEY_DIR

    dir_path = _ensure_key_dir(key_dir)
    priv_path = dir_path / _PRIVATE_KEY_FILENAME
    pub_path = dir_path / _PUBLIC_KEY_FILENAME

    if priv_path.exists() and pub_path.exists():
        # Load and parse existing keys.
        priv_bytes = _pem_unwrap(
            _PRIVATE_PEM_HEADER, _PRIVATE_PEM_FOOTER, priv_path.read_text()
        )
        pub_bytes = _pem_unwrap(
            _PUBLIC_PEM_HEADER, _PUBLIC_PEM_FOOTER, pub_path.read_text()
        )
        if len(priv_bytes) != 32 or len(pub_bytes) != 32:
            raise ValueError(
                f"Existing key files at {dir_path} have wrong byte length "
                f"(priv={len(priv_bytes)}, pub={len(pub_bytes)}); "
                f"expected 32 each. Refusing to overwrite. Move them aside "
                f"or delete them to regenerate."
            )
        # Cross-check: deriving the public key from the seed should match
        # the public key file. If not, the two files don't belong together.
        derived_pub = SigningKey(priv_bytes).verify_key.encode()
        if derived_pub != pub_bytes:
            raise ValueError(
                f"Existing private+public key files at {dir_path} do NOT "
                f"match (deriving the public key from the private file "
                f"yields a different value). Refusing to overwrite. Move "
                f"them aside or delete them to regenerate."
            )
        return {
            "private_key_b64": base64.b64encode(priv_bytes).decode("ascii"),
            "public_key_b64": base64.b64encode(pub_bytes).decode("ascii"),
            "fingerprint": _public_key_fingerprint(pub_bytes),
            "key_dir": str(dir_path),
            "private_key_path": str(priv_path),
            "public_key_path": str(pub_path),
            "generated": False,
        }

    if priv_path.exists() or pub_path.exists():
        # Asymmetric: one exists, the other doesn't. Bail loud — this is
        # an operator error and silently regenerating would lose history.
        raise ValueError(
            f"One of the two key files exists at {dir_path} but not the "
            f"other (priv={priv_path.exists()}, pub={pub_path.exists()}). "
            f"Refusing to regenerate. Move the lone file aside or delete it."
        )

    # Fresh generation path.
    signing_key = SigningKey.generate()
    priv_bytes = signing_key.encode()                 # 32-byte seed
    pub_bytes = signing_key.verify_key.encode()       # 32-byte public key

    priv_path.write_text(
        _pem_wrap(_PRIVATE_PEM_HEADER, _PRIVATE_PEM_FOOTER, priv_bytes)
    )
    pub_path.write_text(
        _pem_wrap(_PUBLIC_PEM_HEADER, _PUBLIC_PEM_FOOTER, pub_bytes)
    )
    try:
        os.chmod(priv_path, 0o600)
    except OSError as exc:
        logger.warning("Could not chmod 600 on %s: %s", priv_path, exc)
    # Leave the public key at umask default (typically 644).

    return {
        "private_key_b64": base64.b64encode(priv_bytes).decode("ascii"),
        "public_key_b64": base64.b64encode(pub_bytes).decode("ascii"),
        "fingerprint": _public_key_fingerprint(pub_bytes),
        "key_dir": str(dir_path),
        "private_key_path": str(priv_path),
        "public_key_path": str(pub_path),
        "generated": True,
    }


# ---------------------------------------------------------------------------
# Well-known document
# ---------------------------------------------------------------------------

def build_well_known_doc(
    public_key_b64: str,
    node_domain: str,
    spec_version: str = None,
    software_version: str = "stage-2a",
) -> dict:
    """
    Build the JSON document served at GET /.well-known/ubi-node.

    Public-key encoding choice: base64 (RFC 4648 standard, URL-safe-ish,
    matches the format pynacl gives us). Documented in DEPLOY.md so a
    peer-implementer doesn't have to guess.

    `endpoints` are placeholders — stage 2b implements them. Listing them
    now lets peer operators write integrations against a stable shape.
    """
    if spec_version is None:
        spec_version = config.FEDERATION_SPEC_VERSION
    return {
        "spec_version": spec_version,
        "node_domain": node_domain,
        "node_public_key": public_key_b64,
        "public_key_encoding": "base64",
        "signature_algorithm": "ed25519",
        "endpoints": {
            # Stage 2b will turn these into live handlers. Until then they
            # 404 — peers that try to call them will see that explicitly.
            "transfer": "/federation/transfer",
            "transfer_confirm": "/federation/transfer/confirm",
        },
        "software": {
            "name": "ubi-bot",
            "version": software_version,
        },
        "created_at": int(time.time()),
    }


# ---------------------------------------------------------------------------
# Discovery + validation
# ---------------------------------------------------------------------------

WELL_KNOWN_PATH = "/.well-known/ubi-node"


def validate_well_known_doc(doc: dict) -> tuple[bool, str]:
    """
    Sanity-check a JSON doc that came back from /.well-known/ubi-node.

    Returns (ok, reason). On success reason="". On failure reason is a
    short human-readable explanation suitable for surfacing to an operator
    via Telegram.

    Stage 2a checks: shape only. No cryptographic verification — the
    public key is taken at face value here.
    """
    if not isinstance(doc, dict):
        return False, "well-known doc is not a JSON object"

    required_str = ("spec_version", "node_domain", "node_public_key")
    for field in required_str:
        if field not in doc:
            return False, f"missing required field: {field}"
        if not isinstance(doc[field], str) or not doc[field].strip():
            return False, f"field {field} is not a non-empty string"

    # The public key has to be valid base64 and decode to exactly 32 bytes.
    try:
        raw = base64.b64decode(doc["node_public_key"], validate=True)
    except Exception as exc:
        return False, f"node_public_key is not valid base64: {exc}"
    if len(raw) != 32:
        return False, (
            f"node_public_key decodes to {len(raw)} bytes, expected 32 "
            f"(Ed25519 keys are 32 bytes)"
        )
    # And it has to be a syntactically valid Ed25519 point.
    try:
        VerifyKey(raw)
    except Exception as exc:
        return False, f"node_public_key is not a valid Ed25519 key: {exc}"

    # spec_version is loose for now — anything starting with "ubi-fed-" is
    # accepted. We'll tighten this when we have multiple versions in flight.
    if not doc["spec_version"].startswith("ubi-fed-"):
        return False, (
            f"unexpected spec_version {doc['spec_version']!r} "
            f"(expected something starting with 'ubi-fed-')"
        )

    return True, ""


async def discover_peer(
    domain: str,
    timeout_seconds: float = 10.0,
    scheme: str = "https",
) -> dict:
    """
    Fetch and parse `https://<domain>/.well-known/ubi-node`.

    Returns the parsed JSON dict on success.
    Raises one of:
      - aiohttp.ClientError (network errors)
      - asyncio.TimeoutError (operator can interpret as "peer unreachable")
      - ValueError (HTTP non-2xx, or response is not valid JSON, or the
        doc fails validate_well_known_doc)

    The `scheme` parameter exists so local tests can hit `http://` against a
    mock server on a different port. Production callers always use `https`.

    Domain canonicalisation: we strip any leading scheme and trailing slash
    so an operator typing `/peer_add https://tie.ubi.asia/` does the right
    thing. The result is stored as a bare lowercase host string.
    """
    canonical_domain = _canonical_domain(domain)
    url = f"{scheme}://{canonical_domain}{WELL_KNOWN_PATH}"

    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                # Read body for the error message (limit to a few KB so a
                # malicious peer can't blow up our log).
                body_preview = (await resp.text())[:500]
                raise ValueError(
                    f"GET {url} returned HTTP {resp.status}: "
                    f"{body_preview!r}"
                )
            try:
                doc = await resp.json(content_type=None)
            except Exception as exc:
                raise ValueError(
                    f"GET {url} returned 200 but body is not valid JSON: {exc}"
                ) from exc

    ok, reason = validate_well_known_doc(doc)
    if not ok:
        raise ValueError(f"well-known doc from {url} failed validation: {reason}")
    return doc


def _canonical_domain(raw: str) -> str:
    """
    Normalise an operator-typed domain into the canonical host form.

    Examples:
        "https://tie.ubi.asia/"  -> "tie.ubi.asia"
        "TIE.ubi.asia"           -> "tie.ubi.asia"
        "tie.ubi.asia:8765"      -> "tie.ubi.asia:8765"  (port preserved)
        "  tie.ubi.asia  "       -> "tie.ubi.asia"
    """
    s = raw.strip().lower()
    for scheme in ("https://", "http://"):
        if s.startswith(scheme):
            s = s[len(scheme) :]
            break
    s = s.rstrip("/")
    # Discard anything after the first path segment (defensive — well-known
    # is always relative to the host root).
    if "/" in s:
        s = s.split("/", 1)[0]
    return s


# ---------------------------------------------------------------------------
# DB helpers — peer_nodes upsert / list / soft-delete
# ---------------------------------------------------------------------------

async def upsert_peer_node(
    db_connection,
    domain: str,
    public_key_b64: str,
    well_known_doc: dict,
    status: str = "active",
) -> dict:
    """
    Insert-or-update a row in peer_nodes for `domain`.

    Idempotent: re-running on the same domain updates `public_key`,
    `last_seen_at`, `metadata`, and (if status='active') re-arms the row.
    Re-running NEVER duplicates — the `domain` column is UNIQUE.

    `db_connection` is an aiosqlite connection (not a fresh one — caller
    owns the connection lifetime; we don't commit here, that's also the
    caller's job, so this can compose into larger transactions).

    Returns a dict snapshot of the resulting row (post-upsert).
    """
    now = int(time.time())
    metadata_json = json.dumps(well_known_doc, separators=(",", ":"), sort_keys=True)

    # Try INSERT first; if it collides on the UNIQUE domain, UPDATE.
    # We use ON CONFLICT (sqlite >= 3.24) — confirmed available in the
    # aiosqlite versions we use elsewhere in this codebase.
    await db_connection.execute(
        """
        INSERT INTO peer_nodes (
            domain, discovered_at, last_seen_at, status, public_key, metadata
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(domain) DO UPDATE SET
            last_seen_at = excluded.last_seen_at,
            status       = excluded.status,
            public_key   = excluded.public_key,
            metadata     = excluded.metadata
        """,
        (domain, now, now, status, public_key_b64, metadata_json),
    )

    # Read back the (possibly updated) row.
    cursor = await db_connection.execute(
        "SELECT id, domain, discovered_at, last_seen_at, status, public_key, metadata "
        "FROM peer_nodes WHERE domain = ?",
        (domain,),
    )
    row = await cursor.fetchone()
    if row is None:
        # Shouldn't happen — we just inserted. Defensive.
        raise RuntimeError(f"upsert_peer_node: row for {domain!r} not found after insert")
    return {
        "id": row[0],
        "domain": row[1],
        "discovered_at": row[2],
        "last_seen_at": row[3],
        "status": row[4],
        "public_key": row[5],
        "metadata": row[6],
    }


async def list_peer_nodes(db_connection) -> list[dict]:
    """Return every row from peer_nodes, ordered by discovered_at ASC."""
    cursor = await db_connection.execute(
        "SELECT id, domain, discovered_at, last_seen_at, status, public_key "
        "FROM peer_nodes ORDER BY discovered_at ASC"
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": r[0],
            "domain": r[1],
            "discovered_at": r[2],
            "last_seen_at": r[3],
            "status": r[4],
            "public_key": r[5],
        }
        for r in rows
    ]


async def soft_remove_peer_node(db_connection, domain: str) -> bool:
    """
    Mark a peer as defederated. Preserves the row (and its history) — we
    never DELETE peer rows because future audit / dispute resolution may
    need them.

    Returns True if a row was updated, False if no peer with that domain.
    """
    canonical = _canonical_domain(domain)
    cursor = await db_connection.execute(
        "UPDATE peer_nodes SET status = 'defederated' WHERE domain = ?",
        (canonical,),
    )
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# aiohttp app — the HTTP server side
# ---------------------------------------------------------------------------

def build_http_app(well_known_doc: dict) -> web.Application:
    """
    Build the aiohttp application that serves `/.well-known/ubi-node`.

    `well_known_doc` is a snapshot taken at bot startup. Stage 2a doesn't
    need a live-updating doc — keys don't rotate, version is static. If
    that changes (stage 2b adds endpoints, stage 3 adds key rotation),
    swap in a per-request builder.

    Everything other than the well-known endpoint returns 404. Stage 2b
    adds the transfer endpoints to this same app.
    """
    app = web.Application()

    async def handle_well_known(request: web.Request) -> web.Response:
        logger.info(
            "well-known: %s %s from %s",
            request.method, request.path, request.remote,
        )
        return web.json_response(well_known_doc)

    async def handle_default(request: web.Request) -> web.Response:
        logger.info(
            "404: %s %s from %s",
            request.method, request.path, request.remote,
        )
        return web.json_response(
            {"error": "not_found", "path": request.path},
            status=404,
        )

    app.router.add_get(WELL_KNOWN_PATH, handle_well_known)
    # Catch-all 404 (matches every other path). aiohttp returns its own 404
    # by default but we want consistent JSON and our own log line.
    app.router.add_route("*", "/{tail:.*}", handle_default)

    return app


async def run_http_server(
    well_known_doc: dict,
    host: str = None,
    port: int = None,
) -> web.AppRunner:
    """
    Start the federation HTTP server on `(host, port)` and return the
    AppRunner so the caller can shut it down cleanly.

    This is an async function so it composes with the aiogram polling
    loop — both run as tasks on the same event loop.

    Shutdown: caller invokes `await runner.cleanup()` on bot shutdown.
    """
    if host is None:
        host = config.FEDERATION_HTTP_HOST
    if port is None:
        port = config.FEDERATION_HTTP_PORT

    app = build_http_app(well_known_doc)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    logger.info(
        "Federation HTTP server listening on %s:%d (well-known at %s)",
        host, port, WELL_KNOWN_PATH,
    )
    return runner
