"""Ed25519 keypairs and compact-JWT minting/verification.

The identity primitive for hiveloom's (explicitly non-production) HTTP control
plane bearer auth: ``cryptography`` generates and serializes Ed25519 keys
(PyJWT does not generate keys); PyJWT mints and verifies the compact JWT
itself, using the ``EdDSA`` algorithm.

Non-production by design: tokens default to a short 15-minute TTL because
there is no revocation-propagation or replay cache (see ``auth.py`` and
``docs/control-plane.md`` for the full limitations list).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from jwt.utils import base64url_decode, base64url_encode

# Hardcoded allow-list — NEVER derive this from the token's own header. A
# token's `alg` header is attacker-controlled input; trusting it to pick the
# verification algorithm is the classic algorithm-confusion attack (e.g.
# craft `alg: HS256` and use the Ed25519 public key bytes as the HMAC
# secret). See test_serve_auth.py's algorithm-confusion regression test.
_ALGORITHM = "EdDSA"

# Short by design: there is no revocation-propagation or replay cache, so a
# captured token is replayable until it expires. 15 minutes bounds that.
DEFAULT_TTL_SECONDS = 900


def generate_keypair() -> tuple[str, str]:
    """Generate an Ed25519 keypair: ``(private_key_pem, public_key_b64)``.

    The private key is unencrypted PKCS8 PEM — custody (who holds it, and how
    it's written to disk) is the caller's job; see ``cli.py``'s ``keys
    generate`` for the 0600-permission write. The public key is its raw
    32-byte point, urlsafe-base64 encoded without padding.
    """
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private_pem, base64url_encode(public_raw).decode("ascii")


def public_key_b64_for(private_key_pem: str) -> str:
    """Recover the public key (b64) matching a private key PEM.

    ``keys sign`` only takes a private-key path (no separate public-key
    flag), so the key_id embedded in a minted token is derived here rather
    than passed in — it must match the key_id the operator computed from the
    public key alone at ``keys authorize`` time.
    """
    private_key = serialization.load_pem_private_key(
        private_key_pem.encode("ascii"), password=None
    )
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("private key is not an Ed25519 key")
    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64url_encode(public_raw).decode("ascii")


def key_id_for(public_key_b64: str) -> str:
    """Short id for a public key: ``sha256(raw pubkey bytes)[:12]`` hex.

    Matches the short-hash convention in ``logging/trace.py:spec_version_hash``.
    """
    return hashlib.sha256(base64url_decode(public_key_b64)).hexdigest()[:12]


def sign_token(
    private_key_pem: str,
    *,
    key_id: str,
    subject: str,
    scope: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> str:
    """Mint a compact EdDSA JWT with claims ``{kid, sub, scope, iat, exp}``."""
    now = datetime.now(UTC)
    claims = {
        "kid": key_id,
        "sub": subject,
        "scope": scope,
        "iat": now,
        "exp": now + timedelta(seconds=ttl_seconds),
    }
    return jwt.encode(claims, private_key_pem, algorithm=_ALGORITHM, headers={"kid": key_id})


def decode_token(token: str, *, public_key_b64: str, leeway_seconds: int = 30) -> dict:
    """Verify and decode a token, raising a ``jwt.PyJWTError`` subclass on failure.

    ``algorithms=[_ALGORITHM]`` is the hardcoded allow-list described above —
    it must never be replaced with a value read from the token itself.
    """
    public_key = Ed25519PublicKey.from_public_bytes(base64url_decode(public_key_b64))
    return jwt.decode(token, public_key, algorithms=[_ALGORITHM], leeway=leeway_seconds)


def unverified_key_id(token: str) -> str | None:
    """Read ``kid`` from the token header, or ``None`` if absent/unparseable.

    This only SELECTS a candidate key to verify against — the header is
    unauthenticated attacker-controlled data at this point. It must never be
    treated as proof of identity; ``decode_token``'s signature check is what
    actually authenticates the token.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        return None
    kid = header.get("kid")
    return kid if isinstance(kid, str) else None
