"""Tests for bearer-token issuance (#26).

Covers the sign+verify round-trip, audience scoping, forged-token rejection
(wrong key), TTL/expiry, anonymous-caller refusal, identity parsing, and the
middleware shim that wires the ContextVar-bound identity to token issuance.
"""

from __future__ import annotations

import time

import jwt
import pytest

from zelosmcp.auth import middleware as auth_mw
from zelosmcp.auth.identity import (
    CallerIdentity,
    current_identity,
    identity_from_scope_headers,
    parse_identity,
)
from zelosmcp.auth.token import (
    AUDIENCE_BACKPLANE,
    AUDIENCE_BROKER,
    TokenError,
    authorization_header,
    issue_token,
    verify_token,
)

KEY = "test-signing-key-which-is-32-bytes!!"


# ── Identity parsing ────────────────────────────────────────────────────


def test_parse_identity_splits_scopes():
    ident = parse_identity("user-42", "read write admin")
    assert ident.subject == "user-42"
    assert ident.scopes == ("read", "write", "admin")
    assert ident.is_authenticated
    assert ident.scope_str == "read write admin"


def test_parse_identity_anonymous_when_empty():
    ident = parse_identity(None, None)
    assert ident.subject == ""
    assert ident.scopes == ()
    assert not ident.is_authenticated


def test_identity_from_scope_headers():
    headers = [
        (b"x-zelos-subject", b"alice"),
        (b"x-zelos-scopes", b"a b"),
        (b"authorization", b"Bearer ignored"),
    ]
    ident = identity_from_scope_headers(headers)
    assert ident.subject == "alice"
    assert ident.scopes == ("a", "b")


def test_identity_from_scope_headers_missing_is_anonymous():
    ident = identity_from_scope_headers([(b"content-type", b"application/json")])
    assert not ident.is_authenticated


# ── Sign + verify round-trip ──────────────────────────────────────────────


@pytest.mark.parametrize("audience", [AUDIENCE_BROKER, AUDIENCE_BACKPLANE])
def test_sign_verify_roundtrip(audience):
    ident = CallerIdentity(subject="user-1", scopes=("scope-a", "scope-b"))
    token = issue_token(ident, audience, signing_key=KEY)
    claims = verify_token(token, audience, signing_key=KEY)
    assert claims["sub"] == "user-1"
    assert claims["scopes"] == "scope-a scope-b"
    assert claims["aud"] == audience
    assert "iat" in claims and "exp" in claims


def test_forged_token_wrong_key_rejected():
    ident = CallerIdentity(subject="user-1")
    token = issue_token(ident, AUDIENCE_BROKER, signing_key=KEY)
    with pytest.raises(jwt.InvalidSignatureError):
        verify_token(token, AUDIENCE_BROKER, signing_key="a-different-key-32-bytes-long!!!")


def test_token_for_broker_rejected_against_backplane():
    ident = CallerIdentity(subject="user-1")
    token = issue_token(ident, AUDIENCE_BROKER, signing_key=KEY)
    with pytest.raises(jwt.InvalidAudienceError):
        verify_token(token, AUDIENCE_BACKPLANE, signing_key=KEY)


def test_expired_token_rejected():
    ident = CallerIdentity(subject="user-1")
    token = issue_token(
        ident, AUDIENCE_BROKER, ttl_seconds=1, signing_key=KEY, now=time.time() - 100
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        verify_token(token, AUDIENCE_BROKER, signing_key=KEY)


def test_default_ttl_is_60s():
    ident = CallerIdentity(subject="user-1")
    token = issue_token(ident, AUDIENCE_BROKER, signing_key=KEY, now=1000)
    # Decode without exp enforcement just to inspect the claim arithmetic
    # (the issued token is intentionally far in the past via now=1000).
    claims = jwt.decode(
        token,
        KEY,
        algorithms=["HS256"],
        audience=AUDIENCE_BROKER,
        options={"verify_exp": False},
    )
    assert claims["exp"] - claims["iat"] == 60


def test_anonymous_caller_refused():
    with pytest.raises(TokenError):
        issue_token(CallerIdentity(), AUDIENCE_BROKER, signing_key=KEY)


def test_unknown_audience_refused():
    with pytest.raises(TokenError):
        issue_token(CallerIdentity(subject="u"), "nope", signing_key=KEY)


def test_no_signing_key_configured_raises(monkeypatch):
    monkeypatch.delenv("ZELOS_INTERNAL_SIGNING_KEY", raising=False)
    monkeypatch.delenv("ZELOS_INTERNAL_SIGNING_KEY_FILE", raising=False)
    with pytest.raises(TokenError):
        issue_token(CallerIdentity(subject="u"), AUDIENCE_BROKER)


def test_signing_key_loaded_from_env(monkeypatch):
    monkeypatch.setenv("ZELOS_INTERNAL_SIGNING_KEY", KEY)
    ident = CallerIdentity(subject="env-user")
    token = issue_token(ident, AUDIENCE_BROKER)
    assert verify_token(token, AUDIENCE_BROKER, signing_key=KEY)["sub"] == "env-user"


def test_signing_key_loaded_from_file(monkeypatch, tmp_path):
    monkeypatch.delenv("ZELOS_INTERNAL_SIGNING_KEY", raising=False)
    key_file = tmp_path / "key"
    key_file.write_text(KEY + "\n")
    monkeypatch.setenv("ZELOS_INTERNAL_SIGNING_KEY_FILE", str(key_file))
    ident = CallerIdentity(subject="file-user")
    token = issue_token(ident, AUDIENCE_BROKER)
    assert verify_token(token, AUDIENCE_BROKER, signing_key=KEY)["sub"] == "file-user"


def test_authorization_header_shape():
    assert authorization_header("abc") == {"Authorization": "Bearer abc"}


# ── Middleware shim (reads the ContextVar-bound identity) ─────────────────


def test_middleware_resolves_bound_identity():
    ident = CallerIdentity(subject="ctx-user", scopes=("s",))
    tok = current_identity.set(ident)
    try:
        assert auth_mw.resolve_identity() is ident
        broker_tok = auth_mw.issue_broker_token(signing_key=KEY)
        bp_tok = auth_mw.issue_backplane_token(signing_key=KEY)
        assert verify_token(broker_tok, AUDIENCE_BROKER, signing_key=KEY)["sub"] == "ctx-user"
        assert verify_token(bp_tok, AUDIENCE_BACKPLANE, signing_key=KEY)["sub"] == "ctx-user"
    finally:
        current_identity.reset(tok)


def test_middleware_broker_auth_header_round_trip():
    ident = CallerIdentity(subject="hdr-user")
    header = auth_mw.broker_auth_header(identity=ident, signing_key=KEY)
    assert header["Authorization"].startswith("Bearer ")
    token = header["Authorization"].split(" ", 1)[1]
    assert verify_token(token, AUDIENCE_BROKER, signing_key=KEY)["sub"] == "hdr-user"


def test_middleware_anonymous_default_refuses_issuance():
    # No identity bound -> resolves anonymous -> issuance refused.
    assert not auth_mw.resolve_identity().is_authenticated
    with pytest.raises(TokenError):
        auth_mw.issue_broker_token(signing_key=KEY)
