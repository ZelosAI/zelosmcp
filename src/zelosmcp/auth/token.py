"""Short-lived bearer-token issuance to the broker and backplane (#26).

When zelosmcp acts on behalf of a user — opening a broker sync channel
(#24) or publishing a backplane inference request (#25) — it must forward a
credential the downstream accepts. The gateway terminates OIDC and injects
the caller's identity as internal headers (``X-Zelos-Subject`` /
``X-Zelos-Scopes``, see :mod:`zelosmcp.auth.identity`); this module exchanges
that identity for a short-lived signed bearer token.

EA approach (per the issue's "Decisions"):

* **HS256 shared-secret JWT.** The same ``ZELOS_INTERNAL_SIGNING_KEY`` is
  configured on gateway, mcp, broker, and backplane. RS256 + JWKS is deferred
  to v1.0 multi-tenancy.
* **Claim shape:** ``sub``, ``scopes`` (space-delimited string, mirroring the
  OIDC convention), ``iat``, ``exp``, and ``aud`` (``"broker"`` or
  ``"backplane"``).
* **TTL:** 60 s, re-issued per invocation. No refresh-token semantics in EA.

Verification (used by tests and, in a real deployment, by the broker /
backplane) lives in :func:`verify_token` so the sign+verify round-trip and
the forged-token rejection can be exercised without a live downstream.
"""

from __future__ import annotations

import os
import time
from typing import Any

import jwt

from zelosmcp.auth.identity import CallerIdentity

# Env var holding the shared HS256 signing secret. Bootstrapped by the
# operator's Secret (matches the per-strategy bundle Secret examples in
# zelosai #43 / #46 / #49). ``*_FILE`` fallback follows the suite container
# contract so the key can be mounted from a file instead of an env value.
SIGNING_KEY_ENV = "ZELOS_INTERNAL_SIGNING_KEY"
SIGNING_KEY_FILE_ENV = "ZELOS_INTERNAL_SIGNING_KEY_FILE"

ALGORITHM = "HS256"

# 60 s sliding TTL, re-issued per invocation (issue: "60s sliding").
DEFAULT_TTL_SECONDS = 60

# Recognised audiences. The downstream validates ``aud`` so a token minted
# for the broker can't be replayed against the backplane and vice versa.
AUDIENCE_BROKER = "broker"
AUDIENCE_BACKPLANE = "backplane"
_VALID_AUDIENCES = frozenset({AUDIENCE_BROKER, AUDIENCE_BACKPLANE})


class TokenError(RuntimeError):
    """Raised when a token cannot be issued (no identity / no signing key)."""


def _load_signing_key() -> str:
    """Resolve the shared signing secret from env or a mounted file.

    Raises :class:`TokenError` when neither is set — a deployment that wants
    the data path MUST configure the key, and failing loudly here is better
    than minting unverifiable tokens.
    """
    key = os.environ.get(SIGNING_KEY_ENV)
    if key:
        return key
    path = os.environ.get(SIGNING_KEY_FILE_ENV)
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                contents = f.read().strip()
        except OSError as exc:  # pragma: no cover - I/O error path
            raise TokenError(
                f"{SIGNING_KEY_FILE_ENV}={path!r} could not be read: {exc}"
            ) from exc
        if contents:
            return contents
    raise TokenError(
        f"no internal signing key configured: set {SIGNING_KEY_ENV} or "
        f"{SIGNING_KEY_FILE_ENV}"
    )


def issue_token(
    identity: CallerIdentity,
    audience: str,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    signing_key: str | None = None,
    now: float | None = None,
) -> str:
    """Mint an HS256 bearer token for ``identity`` scoped to ``audience``.

    ``audience`` must be :data:`AUDIENCE_BROKER` or :data:`AUDIENCE_BACKPLANE`.
    The caller must be authenticated (a gateway-issued subject); an anonymous
    identity raises :class:`TokenError` so we never forward an unauthenticated
    request downstream.

    ``signing_key`` overrides the env-resolved key (used by tests); ``now``
    overrides the clock (used by tests to assert ``exp``).
    """
    if audience not in _VALID_AUDIENCES:
        raise TokenError(
            f"unknown audience {audience!r}; expected one of "
            f"{sorted(_VALID_AUDIENCES)}"
        )
    if not identity.is_authenticated:
        raise TokenError(
            "cannot issue a downstream token for an anonymous caller; the "
            "gateway must inject X-Zelos-Subject"
        )
    key = signing_key if signing_key is not None else _load_signing_key()
    issued_at = int(now if now is not None else time.time())
    claims: dict[str, Any] = {
        "sub": identity.subject,
        "scopes": identity.scope_str,
        "iat": issued_at,
        "exp": issued_at + ttl_seconds,
        "aud": audience,
    }
    return jwt.encode(claims, key, algorithm=ALGORITHM)


def verify_token(
    token: str,
    audience: str,
    *,
    signing_key: str | None = None,
    leeway: int = 0,
) -> dict[str, Any]:
    """Verify ``token`` for ``audience`` and return its claims.

    This is what the broker / backplane run on the receiving end. It is also
    used by the unit tests to assert the sign+verify round-trip and that a
    token signed with the wrong key (forged) is rejected.

    Raises :class:`jwt.InvalidTokenError` (or a subclass — e.g.
    ``ExpiredSignatureError``, ``InvalidSignatureError``, ``InvalidAudienceError``)
    on any failure.
    """
    key = signing_key if signing_key is not None else _load_signing_key()
    return jwt.decode(
        token,
        key,
        algorithms=[ALGORITHM],
        audience=audience,
        leeway=leeway,
        options={"require": ["sub", "iat", "exp", "aud"]},
    )


def authorization_header(token: str) -> dict[str, str]:
    """Return the ``Authorization: Bearer <token>`` header dict for ``token``."""
    return {"Authorization": f"Bearer {token}"}
