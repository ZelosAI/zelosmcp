"""Bearer-token attachment for outgoing broker + backplane calls (#26).

The issue calls for "FastMCP middleware that wraps tool invocations, attaches
the token to outgoing broker + backplane calls". zelosmcp does not run
FastMCP — it uses a lowlevel ``mcp.server.Server`` (see
:mod:`zelosmcp.builtin`) and routes raw ASGI through the dispatcher in
:mod:`zelosmcp.app`. The equivalent seam here is:

1. The ASGI dispatcher reads the gateway's ``X-Zelos-*`` identity headers and
   binds them to the :data:`zelosmcp.auth.identity.current_identity`
   ContextVar (writer side — wired in :mod:`zelosmcp.app`).
2. Data-path tool handlers (#24 sync subagents, #25 async tasks) call into
   this module to mint and attach the per-invocation bearer token to their
   broker / backplane calls (reader side).

This module is that reader-side helper: a tiny, dependency-free shim that
resolves the bound identity and issues an audience-scoped token. Keeping it
separate from :mod:`zelosmcp.auth.token` lets the broker client / backplane
publisher stay agnostic of *where* the identity came from.
"""

from __future__ import annotations

from zelosmcp.auth import token as token_mod
from zelosmcp.auth.identity import CallerIdentity, current_identity


def resolve_identity() -> CallerIdentity:
    """Return the identity bound for the current invocation.

    Falls back to the anonymous identity when no gateway headers were
    propagated (e.g. a direct dev call). The token issuer refuses to mint a
    credential for an anonymous identity, so the unauthenticated case fails
    closed at issuance time rather than silently sending an empty token.
    """
    return current_identity.get()


def issue_broker_token(
    *,
    identity: CallerIdentity | None = None,
    signing_key: str | None = None,
) -> str:
    """Mint a broker-audience bearer token for the current (or given) caller."""
    ident = identity if identity is not None else resolve_identity()
    return token_mod.issue_token(
        ident,
        token_mod.AUDIENCE_BROKER,
        signing_key=signing_key,
    )


def issue_backplane_token(
    *,
    identity: CallerIdentity | None = None,
    signing_key: str | None = None,
) -> str:
    """Mint a backplane-audience bearer token for the current (or given) caller."""
    ident = identity if identity is not None else resolve_identity()
    return token_mod.issue_token(
        ident,
        token_mod.AUDIENCE_BACKPLANE,
        signing_key=signing_key,
    )


def broker_auth_header(
    *,
    identity: CallerIdentity | None = None,
    signing_key: str | None = None,
) -> dict[str, str]:
    """``Authorization: Bearer <broker-token>`` header for the current caller."""
    return token_mod.authorization_header(
        issue_broker_token(identity=identity, signing_key=signing_key)
    )
