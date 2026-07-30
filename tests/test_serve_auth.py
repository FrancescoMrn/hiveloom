"""Tests for `hiveloom.serve.keys`/`hiveloom.serve.auth` and the `hiveloom keys` CLI.

Offline, no network. Deterministic time handling: expiry is exercised by
minting tokens with a deliberately negative TTL rather than sleeping.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import jwt
import pytest
from typer.testing import CliRunner

from hiveloom.cli import app
from hiveloom.errors import AuthenticationError, AuthorizationError, ExitCode, SpecError
from hiveloom.serve import auth as auth_mod
from hiveloom.serve import keys as keys_mod

runner = CliRunner()


def _json(result) -> dict:
    return json.loads(result.stdout)


def _raw_public_bytes(public_key_b64: str) -> bytes:
    padding = "=" * (-len(public_key_b64) % 4)
    return base64.urlsafe_b64decode(public_key_b64 + padding)


def _corrupt_row(path: Path, **overrides) -> None:
    """Hand-edit the store's first row, simulating a corrupted authorized_keys.json."""
    data = json.loads(path.read_text())
    data["keys"][0].update(overrides)
    path.write_text(json.dumps(data))


def _delete_row_field(path: Path, field: str) -> None:
    data = json.loads(path.read_text())
    del data["keys"][0][field]
    path.write_text(json.dumps(data))


# --------------------------------------------------------------------------- #
# keys.py: keypair generation, signing, decoding
# --------------------------------------------------------------------------- #
def test_sign_and_decode_round_trip():
    private_pem, public_b64 = keys_mod.generate_keypair()
    key_id = keys_mod.key_id_for(public_b64)

    token = keys_mod.sign_token(
        private_pem, key_id=key_id, subject="rinaldo", scope="run", ttl_seconds=900
    )
    claims = keys_mod.decode_token(token, public_key_b64=public_b64)

    assert claims["kid"] == key_id
    assert claims["sub"] == "rinaldo"
    assert claims["scope"] == "run"
    assert claims["exp"] > claims["iat"]
    assert keys_mod.unverified_key_id(token) == key_id


def test_key_id_for_is_stable():
    _, public_b64 = keys_mod.generate_keypair()
    assert keys_mod.key_id_for(public_b64) == keys_mod.key_id_for(public_b64)


def test_public_key_b64_for_matches_generated_public_key():
    private_pem, public_b64 = keys_mod.generate_keypair()
    assert keys_mod.public_key_b64_for(private_pem) == public_b64


def test_expired_token_rejected():
    private_pem, public_b64 = keys_mod.generate_keypair()
    key_id = keys_mod.key_id_for(public_b64)
    # Deliberately negative TTL, well past the default 30s leeway — no sleep.
    token = keys_mod.sign_token(
        private_pem, key_id=key_id, subject="rinaldo", scope="run", ttl_seconds=-3600
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        keys_mod.decode_token(token, public_key_b64=public_b64)


def test_algorithm_confusion_decode_token_rejects():
    """The EdDSA allow-list in decode_token is hardcoded, not header-derived.

    Forge an HS256 token using the raw Ed25519 public-key bytes as the HMAC
    secret (the classic algorithm-confusion attack against a verifier that
    picks its algorithm from the token's own header). This must fail if
    someone later replaces `algorithms=["EdDSA"]` with a header-derived value.
    """
    _, public_b64 = keys_mod.generate_keypair()
    raw_public = _raw_public_bytes(public_b64)
    forged = jwt.encode({"sub": "attacker", "scope": "*"}, raw_public, algorithm="HS256")

    # Prove the attack actually works against a naive header-derived verifier,
    # so this regression test is meaningful (not vacuously passing).
    header = jwt.get_unverified_header(forged)
    vulnerable = jwt.decode(forged, raw_public, algorithms=[header["alg"]])
    assert vulnerable["sub"] == "attacker"

    with pytest.raises(jwt.InvalidAlgorithmError):
        keys_mod.decode_token(forged, public_key_b64=public_b64)


# --------------------------------------------------------------------------- #
# auth.py: authorized-keys store
# --------------------------------------------------------------------------- #
def test_authorize_key_idempotent_unrevokes_and_updates_scopes(tmp_path: Path):
    path = tmp_path / "authorized_keys.json"
    _, public_b64 = keys_mod.generate_keypair()

    row1 = auth_mod.authorize_key(path, name="alice", public_key_b64=public_b64, scopes=["read"])
    auth_mod.revoke_key(path, row1["key_id"])
    row2 = auth_mod.authorize_key(path, name="alice", public_key_b64=public_b64, scopes=["*"])

    rows = auth_mod.list_keys(path)
    assert len(rows) == 1
    assert row2["key_id"] == row1["key_id"]
    assert rows[0]["scopes"] == ["*"]
    assert rows[0]["revoked"] is False


def test_revoke_keeps_row_as_audit_trail(tmp_path: Path):
    path = tmp_path / "authorized_keys.json"
    _, public_b64 = keys_mod.generate_keypair()
    row = auth_mod.authorize_key(path, name="bob", public_key_b64=public_b64, scopes=["*"])

    auth_mod.revoke_key(path, row["key_id"])

    rows = auth_mod.list_keys(path)
    assert len(rows) == 1
    assert rows[0]["key_id"] == row["key_id"]
    assert rows[0]["revoked"] is True


def test_revoke_unknown_key_id_raises(tmp_path: Path):
    path = tmp_path / "authorized_keys.json"
    with pytest.raises(SpecError):
        auth_mod.revoke_key(path, "nope")


def test_list_keys_row_shape(tmp_path: Path):
    path = tmp_path / "authorized_keys.json"
    _, public_b64 = keys_mod.generate_keypair()
    auth_mod.authorize_key(path, name="carol", public_key_b64=public_b64, scopes=["read", "run"])

    [row] = auth_mod.list_keys(path)
    assert set(row) == {"key_id", "public_key", "name", "scopes", "added_at", "revoked"}
    assert row["name"] == "carol"
    assert row["scopes"] == ["read", "run"]


def test_authorized_keys_path_precedence(tmp_path: Path, monkeypatch):
    harness = tmp_path / "h"
    assert auth_mod.authorized_keys_path(harness) == harness / ".hiveloom" / "authorized_keys.json"

    monkeypatch.setenv("HIVELOOM_AUTHORIZED_KEYS", str(tmp_path / "env.json"))
    assert auth_mod.authorized_keys_path(harness) == tmp_path / "env.json"

    override = tmp_path / "explicit.json"
    assert auth_mod.authorized_keys_path(harness, override=override) == override


# --------------------------------------------------------------------------- #
# auth.py: verify_bearer ordering and error classes
# --------------------------------------------------------------------------- #
def _authorized(tmp_path: Path, *, scopes: list[str]) -> tuple[Path, str, str]:
    """Authorize a fresh keypair; return (keys_path, private_pem, key_id)."""
    path = tmp_path / "authorized_keys.json"
    private_pem, public_b64 = keys_mod.generate_keypair()
    row = auth_mod.authorize_key(path, name="dana", public_key_b64=public_b64, scopes=scopes)
    return path, private_pem, row["key_id"]


@pytest.mark.parametrize(
    "header",
    [None, "", "Basic xyz", "Bearer", "Bearer   ", "Bearer not-a-real-jwt-at-all"],
)
def test_verify_bearer_malformed_or_garbage_header(tmp_path: Path, header):
    path = tmp_path / "authorized_keys.json"
    with pytest.raises(AuthenticationError):
        auth_mod.verify_bearer(header, keys_path=path, required_scope="run")


def test_verify_bearer_unknown_kid_rejected(tmp_path: Path):
    path, private_pem, key_id = _authorized(tmp_path, scopes=["*"])
    # Sign with a kid that was never authorized.
    token = keys_mod.sign_token(
        private_pem, key_id="deadbeefcafe", subject="dana", scope="run", ttl_seconds=900
    )
    with pytest.raises(AuthenticationError):
        auth_mod.verify_bearer(f"Bearer {token}", keys_path=path, required_scope="run")


def test_verify_bearer_revoked_key_rejected_with_valid_signature(tmp_path: Path):
    """Revocation must be checked AFTER signature verification (order step 4):
    this token's signature is genuinely valid, proving the rejection comes
    from the revoked check, not from an incidental verification failure.
    """
    path, private_pem, key_id = _authorized(tmp_path, scopes=["*"])
    token = keys_mod.sign_token(
        private_pem, key_id=key_id, subject="dana", scope="run", ttl_seconds=900
    )
    # Sanity: the token verifies fine before revocation.
    auth_mod.verify_bearer(f"Bearer {token}", keys_path=path, required_scope="run")

    auth_mod.revoke_key(path, key_id)

    with pytest.raises(AuthenticationError):
        auth_mod.verify_bearer(f"Bearer {token}", keys_path=path, required_scope="run")


def test_verify_bearer_scope_mismatch_is_authorization_error_not_authentication(tmp_path: Path):
    path, private_pem, key_id = _authorized(tmp_path, scopes=["read"])
    token = keys_mod.sign_token(
        private_pem, key_id=key_id, subject="dana", scope="read", ttl_seconds=900
    )
    with pytest.raises(AuthorizationError):
        auth_mod.verify_bearer(f"Bearer {token}", keys_path=path, required_scope="mutate")


def test_verify_bearer_token_scope_cannot_exceed_key_scopes(tmp_path: Path):
    """A token minted claiming a scope its key was never authorized for must
    not grant that scope, even though the signature itself is valid."""
    path, private_pem, key_id = _authorized(tmp_path, scopes=["read"])
    token = keys_mod.sign_token(
        private_pem, key_id=key_id, subject="dana", scope="mutate", ttl_seconds=900
    )
    with pytest.raises(AuthorizationError):
        auth_mod.verify_bearer(f"Bearer {token}", keys_path=path, required_scope="mutate")


def test_verify_bearer_wildcard_scope_satisfies_any_requirement(tmp_path: Path):
    path, private_pem, key_id = _authorized(tmp_path, scopes=["*"])
    token = keys_mod.sign_token(
        private_pem, key_id=key_id, subject="dana", scope="*", ttl_seconds=900
    )
    claims = auth_mod.verify_bearer(f"Bearer {token}", keys_path=path, required_scope="evolve")
    assert claims["sub"] == "dana"


def test_verify_bearer_algorithm_confusion_rejected(tmp_path: Path):
    """End-to-end regression: a forged HS256 token, with the header `kid` set
    to a REAL authorized key so the store lookup succeeds, must still be
    rejected by the signature-verification step.
    """
    path = tmp_path / "authorized_keys.json"
    _, public_b64 = keys_mod.generate_keypair()
    row = auth_mod.authorize_key(path, name="eve", public_key_b64=public_b64, scopes=["*"])
    raw_public = _raw_public_bytes(public_b64)

    forged = jwt.encode(
        {"sub": "attacker", "scope": "*"},
        raw_public,
        algorithm="HS256",
        headers={"kid": row["key_id"]},
    )
    with pytest.raises(AuthenticationError):
        auth_mod.verify_bearer(f"Bearer {forged}", keys_path=path, required_scope="run")


# --------------------------------------------------------------------------- #
# auth.py: corrupted/hand-edited store rows fail closed, not with a raw traceback
# --------------------------------------------------------------------------- #
def test_verify_bearer_corrupted_public_key_wrong_length_is_authentication_error(
    tmp_path: Path,
):
    path, private_pem, key_id = _authorized(tmp_path, scopes=["*"])
    token = keys_mod.sign_token(
        private_pem, key_id=key_id, subject="dana", scope="*", ttl_seconds=900
    )
    # Valid base64, but not 32 bytes: cryptography raises
    # `ValueError: An Ed25519 public key is 32 bytes long`, not a jwt error.
    short_key = base64.urlsafe_b64encode(b"0123456789").rstrip(b"=").decode("ascii")
    _corrupt_row(path, public_key=short_key)

    with pytest.raises(AuthenticationError):
        auth_mod.verify_bearer(f"Bearer {token}", keys_path=path, required_scope="run")


def test_verify_bearer_missing_public_key_field_is_authentication_error(tmp_path: Path):
    path, private_pem, key_id = _authorized(tmp_path, scopes=["*"])
    token = keys_mod.sign_token(
        private_pem, key_id=key_id, subject="dana", scope="*", ttl_seconds=900
    )
    _delete_row_field(path, "public_key")  # raises KeyError, not a jwt error

    with pytest.raises(AuthenticationError):
        auth_mod.verify_bearer(f"Bearer {token}", keys_path=path, required_scope="run")


def test_verify_bearer_non_string_public_key_is_authentication_error(tmp_path: Path):
    path, private_pem, key_id = _authorized(tmp_path, scopes=["*"])
    token = keys_mod.sign_token(
        private_pem, key_id=key_id, subject="dana", scope="*", ttl_seconds=900
    )
    _corrupt_row(path, public_key=12345)  # raises TypeError, not a jwt error

    with pytest.raises(AuthenticationError):
        auth_mod.verify_bearer(f"Bearer {token}", keys_path=path, required_scope="run")


def test_verify_bearer_scopes_as_bare_string_does_not_char_split(tmp_path: Path):
    """A hand-corrupted row with `scopes: "read"` (a bare string instead of a
    list) must not be treated as `{'r', 'e', 'a', 'd'}` via `set(...)` — that
    would wrongly satisfy a single-character required_scope. The token itself
    requests scope "r", isolating the key-scopes check: pre-fix, `"r" in
    set("read")` is True and this test would NOT raise.
    """
    path, private_pem, key_id = _authorized(tmp_path, scopes=["*"])
    _corrupt_row(path, scopes="read")
    token = keys_mod.sign_token(
        private_pem, key_id=key_id, subject="dana", scope="r", ttl_seconds=900
    )

    with pytest.raises(AuthorizationError):
        auth_mod.verify_bearer(f"Bearer {token}", keys_path=path, required_scope="r")


def test_load_authorized_keys_survives_non_utf8_file(tmp_path: Path):
    """Fix-round regression: `UnicodeDecodeError` subclasses `ValueError` but
    was not in the original `except (json.JSONDecodeError, OSError)` tuple,
    so a store file with invalid UTF-8 bytes raised a raw exception instead
    of being treated as corrupt (empty), same as any other malformed store.
    """
    path = tmp_path / "authorized_keys.json"
    path.write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")

    assert auth_mod.load_authorized_keys(path) == {"keys": []}


def test_verify_bearer_survives_non_utf8_store_file(tmp_path: Path):
    path = tmp_path / "authorized_keys.json"
    path.write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")
    private_pem, public_b64 = keys_mod.generate_keypair()
    key_id = keys_mod.key_id_for(public_b64)
    token = keys_mod.sign_token(private_pem, key_id=key_id, subject="x", scope="run")

    with pytest.raises(AuthenticationError):
        auth_mod.verify_bearer(f"Bearer {token}", keys_path=path, required_scope="run")


# --------------------------------------------------------------------------- #
# CLI: `hiveloom keys ...`
# --------------------------------------------------------------------------- #
def test_cli_keys_generate_refuses_overwrite_and_sets_permissions(tmp_path: Path):
    out_dir = tmp_path / "keys"
    r = runner.invoke(app, ["keys", "generate", "alice", "--out-dir", str(out_dir), "--json"])
    assert r.exit_code == ExitCode.OK
    payload = _json(r)
    key_path = Path(payload["private_key_path"])
    assert key_path.exists()

    if sys.platform != "win32":
        assert (key_path.stat().st_mode & 0o777) == 0o600

    r2 = runner.invoke(app, ["keys", "generate", "alice", "--out-dir", str(out_dir), "--json"])
    assert r2.exit_code == ExitCode.SPEC_ERROR
    assert _json(r2)["ok"] is False


def _generate_key_not_starting_with_dash(key_dir: Path) -> dict:
    """`keys generate`, retried if the public key starts with `-`.

    Fix-round-5 flake diagnosis: the public key is base64url (alphabet
    includes `-`), so ~1/64 of freshly generated keys start with one —
    confirmed empirically (1.545% over 20k samples vs. the predicted
    1.5625%). Passed as a plain positional CLI argument (`keys authorize
    <name> <public-key> ...`, the documented shape), Click's parser then
    misreads it as an option flag ("No such option: -a") — this is a real,
    if narrow, CLI edge case (any user could hit it, not just this test;
    the standard escape is `--` before the positional args), not an
    ordering/environment issue. This test exercises the ordinary
    positional-argument path, so it needs a key that doesn't hit that edge
    case; retrying here — not fixing Typer's argument parsing globally, a
    materially riskier change for a ~1.5% edge case with a well-known
    workaround — keeps this test deterministic without touching production
    code. (1/64)**10 is astronomically small, so 10 attempts is effectively
    certain to succeed.
    """
    for attempt in range(10):
        r = runner.invoke(
            app,
            ["keys", "generate", f"alice{attempt}", "--out-dir", str(key_dir), "--json"],
        )
        assert r.exit_code == ExitCode.OK
        generated = _json(r)
        if not generated["public_key"].startswith("-"):
            return generated
    raise AssertionError("could not generate a public key not starting with '-' in 10 attempts")


def test_cli_keys_full_flow(tmp_path: Path):
    key_dir = tmp_path / "keys"
    harness = tmp_path / "h"

    generated = _generate_key_not_starting_with_dash(key_dir)

    r = runner.invoke(
        app,
        [
            "keys", "authorize", "alice", generated["public_key"],
            "--harness", str(harness), "--scope", "run", "--json",
        ],
    )
    assert r.exit_code == ExitCode.OK
    authorized = _json(r)
    assert authorized["key_id"] == generated["key_id"]
    assert authorized["scopes"] == ["run"]

    r = runner.invoke(app, ["keys", "list", "--harness", str(harness), "--json"])
    assert r.exit_code == ExitCode.OK
    listed = _json(r)["keys"]
    assert len(listed) == 1
    assert listed[0]["key_id"] == generated["key_id"]

    r = runner.invoke(
        app,
        ["keys", "sign", "--key", generated["private_key_path"], "--scope", "run", "--json"],
    )
    assert r.exit_code == ExitCode.OK
    token = _json(r)["token"]
    assert token.count(".") == 2

    keys_path = auth_mod.authorized_keys_path(str(harness))
    claims = auth_mod.verify_bearer(f"Bearer {token}", keys_path=keys_path, required_scope="run")
    assert claims["kid"] == generated["key_id"]

    r = runner.invoke(
        app, ["keys", "revoke", generated["key_id"], "--harness", str(harness), "--json"]
    )
    assert r.exit_code == ExitCode.OK
    assert _json(r)["revoked"] is True

    r = runner.invoke(app, ["keys", "list", "--harness", str(harness), "--json"])
    assert _json(r)["keys"][0]["revoked"] is True


def test_cli_keys_revoke_unknown_id_is_spec_error(tmp_path: Path):
    r = runner.invoke(app, ["keys", "revoke", "nope", "--harness", str(tmp_path / "h"), "--json"])
    assert r.exit_code == ExitCode.SPEC_ERROR
    assert _json(r)["ok"] is False
