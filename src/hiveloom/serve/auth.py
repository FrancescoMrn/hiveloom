"""The authorized-keys store and bearer-token request verification.

Mirrors ``trust.py``'s shape: a JSON store with load/save helpers and an env
override. Where ``trust.py`` protects a machine from a *foreign harness*,
this module protects a deployed harness's (future) HTTP control plane from an
*unauthorized caller* — a related but distinct threat model.

Store location: ``<harness_dir>/.hiveloom/authorized_keys.json``. That path is
deliberate — ``package.py``'s exclusion list already drops ``.hiveloom/`` from
zips and the generated ``.dockerignore``, so this file never ships inside a
distributable artifact (see ``package.py``'s ``_EXCLUDE_DIRS``).

Non-production by design, per the module's callers: no TLS, no replay/nonce
cache, no revocation propagation beyond this one file. See
``docs/control-plane.md`` for the full limitations list.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import jwt

from hiveloom.errors import AuthenticationError, AuthorizationError, SpecError
from hiveloom.serve.keys import MAX_TTL_SECONDS, decode_token, key_id_for, unverified_key_id
from hiveloom.spec.loader import atomic_write_text

_ENV_VAR = "HIVELOOM_AUTHORIZED_KEYS"
_BEARER_PREFIX = "Bearer "


def authorized_keys_path(harness_dir: str | Path, *, override: str | Path | None = None) -> Path:
    """Resolve the authorized-keys store path.

    Precedence: an explicit ``override`` (the CLI's ``--authorized-keys``),
    then ``$HIVELOOM_AUTHORIZED_KEYS``, then ``<harness_dir>/.hiveloom/authorized_keys.json``.
    """
    if override is not None:
        return Path(override)
    env = os.environ.get(_ENV_VAR, "").strip()
    if env:
        return Path(env)
    return Path(harness_dir) / ".hiveloom" / "authorized_keys.json"


def load_authorized_keys(path: str | Path) -> dict:
    """Load the store, or an empty one if it doesn't exist or is corrupt."""
    resolved = Path(path)
    if not resolved.exists():
        return {"keys": []}
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        # ValueError covers json.JSONDecodeError (already a ValueError
        # subclass) AND UnicodeDecodeError from a non-UTF-8 store file — both
        # are "corrupt", same as an OSError reading it; every malformed-store
        # shape must fail the same way (an empty store, not a raw traceback).
        return {"keys": []}
    if not isinstance(data, dict) or not isinstance(data.get("keys"), list):
        return {"keys": []}
    return data


def _save(path: str | Path, data: dict) -> None:
    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(resolved, json.dumps(data, indent=2) + "\n")


def authorize_key(
    path: str | Path, *, name: str, public_key_b64: str, scopes: list[str]
) -> dict:
    """Authorize a public key. Idempotent on ``key_id``.

    Re-authorizing an already-known key updates its ``name``/``scopes`` and
    un-revokes it — this is how an operator reinstates a previously revoked
    member without needing a separate "unrevoke" verb.
    """
    data = load_authorized_keys(path)
    key_id = key_id_for(public_key_b64)
    row = {
        "key_id": key_id,
        "public_key": public_key_b64,
        "name": name,
        "scopes": list(scopes),
        "added_at": datetime.now(UTC).isoformat(),
        "revoked": False,
    }
    keys = data["keys"]
    for i, existing in enumerate(keys):
        if existing.get("key_id") == key_id:
            keys[i] = row
            break
    else:
        keys.append(row)
    _save(path, data)
    return row


def revoke_key(path: str | Path, key_id: str) -> None:
    """Mark ``key_id`` revoked. The row is kept (not deleted) as an audit trail."""
    data = load_authorized_keys(path)
    for row in data["keys"]:
        if row.get("key_id") == key_id:
            row["revoked"] = True
            _save(path, data)
            return
    raise SpecError(f"no authorized key with id '{key_id}'")


def list_keys(path: str | Path) -> list[dict]:
    return load_authorized_keys(path)["keys"]


def verify_bearer(
    authorization_header: str | None, *, keys_path: str | Path, required_scope: str
) -> dict:
    """Verify an ``Authorization`` header and return the token's claims.

    Step order is security-critical — each step only trusts what the
    previous one has established:

    1. Reject a missing/malformed header (no ``Bearer `` prefix, or nothing
       after it) outright; there is nothing to look up yet.
    2. ``unverified_key_id`` reads the header's ``kid`` to SELECT a candidate
       key row. An unknown ``kid`` fails here — still before any signature
       check, since we merely need a public key to attempt verification.
    3. ``decode_token`` verifies the signature against that candidate's
       public key, with the hardcoded ``EdDSA`` allow-list. An invalid
       signature or expired token fails here — as does a corrupted store
       row (a garbled, wrong-length, or missing ``public_key`` field):
       those surface as ``ValueError``/``KeyError``/``TypeError`` rather
       than a ``jwt`` exception, but must fail the same way — a typed
       authentication error, never a raw traceback reaching a caller.
    4. Only AFTER the signature is verified do we trust anything read from
       the (until now unauthenticated) token or looked up via its claimed
       ``kid`` — including the key row's ``revoked`` flag. Checking
       revocation any earlier would mean acting on unverified data: an
       attacker could not forge a valid signature, but nothing before this
       point proves the ``kid`` in the header was genuine.
    5. Scope check last, and raising a DISTINCT error class from the
       authentication failures above — the control-plane server maps
       authentication failures to 401 and this to 403.
    """
    if not authorization_header or not authorization_header.startswith(_BEARER_PREFIX):
        raise AuthenticationError("missing or malformed Authorization header")
    token = authorization_header[len(_BEARER_PREFIX) :].strip()
    if not token:
        raise AuthenticationError("missing or malformed Authorization header")

    kid = unverified_key_id(token)
    if kid is None:
        raise AuthenticationError("token has no key id")
    # The store is a hand-editable JSON file: a row could be a non-dict element
    # or lack a string `key_id`. Skip malformed rows rather than KeyError/
    # TypeError out of the index build — an unmatchable row just falls through
    # to the "unknown key id" 401 below, which is the correct fail-closed
    # outcome (deny), not a 500 with a traceback.
    rows = {
        row["key_id"]: row
        for row in list_keys(keys_path)
        if isinstance(row, dict) and isinstance(row.get("key_id"), str)
    }
    row = rows.get(kid)
    if row is None:
        raise AuthenticationError(f"unknown key id '{kid}'")

    try:
        claims = decode_token(token, public_key_b64=row["public_key"])
    except (jwt.PyJWTError, ValueError, KeyError, TypeError) as exc:
        # ValueError/KeyError/TypeError cover a corrupted store row (a
        # garbled, wrong-length, or missing `public_key` field) — the store
        # is a hand-editable JSON file, so this is caller-reachable input,
        # not just a jwt-level failure. Either way it must fail closed as a
        # typed auth error, never leak a raw traceback to the caller.
        raise AuthenticationError(f"invalid token or corrupted key record: {exc}") from exc

    if row.get("revoked", False):
        raise AuthenticationError(f"key '{kid}' has been revoked")

    # Server-side lifetime ceiling: a member mints their own tokens, so the
    # deploy box — not the minter — is where the maximum replay window is
    # enforced. jwt already rejected an expired token; this rejects one whose
    # total lifetime (exp - iat) exceeds the cap. iat is always present on
    # tokens sign_token minted; if a foreign minter omitted it, fall back to
    # bounding the remaining lifetime against now.
    exp, iat = claims.get("exp"), claims.get("iat")
    if exp is not None:
        lifetime = exp - iat if iat is not None else exp - datetime.now(UTC).timestamp()
        if lifetime > MAX_TTL_SECONDS + 30:  # 30s slack for clock skew / leeway
            raise AuthenticationError(
                f"token lifetime exceeds the server maximum of {MAX_TTL_SECONDS}s"
            )

    raw_scopes = row.get("scopes", [])
    # A hand-edited store could set `scopes` to a bare string; `set()` on a
    # string silently splits it into characters (`set("read")` ==
    # `{'r','e','a','d'}`), which could wrongly satisfy a single-character
    # required_scope. Only a real list is meaningful here.
    key_scopes = set(raw_scopes) if isinstance(raw_scopes, list) else set()
    token_scope = claims.get("scope", "")
    # A token cannot grant more than the key it was signed under holds: both
    # the key's authorized scopes AND the token's own scope claim must cover
    # `required_scope` (or be "*").
    key_ok = "*" in key_scopes or required_scope in key_scopes
    token_ok = token_scope == "*" or token_scope == required_scope
    if not (key_ok and token_ok):
        raise AuthorizationError(
            f"key '{kid}' (scopes={sorted(key_scopes)}) and token (scope={token_scope!r}) "
            f"do not both cover required scope '{required_scope}'"
        )
    return claims
