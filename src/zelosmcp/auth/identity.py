"""Per-invocation caller identity propagated from the gateway.

The zelosgateway terminates OIDC and forwards the authenticated caller to
zelosmcp as two internal HTTP headers (see ``zelosai`` ``12-auth.md``):

* ``X-Zelos-Subject`` — the OIDC subject (``sub``) of the caller.
* ``X-Zelos-Scopes``  — a space-separated list of granted scopes.

The MCP SDK is transport-agnostic and does not surface raw HTTP headers to
tool handlers, so — exactly like :data:`zelosmcp.passthrough_pool.inbound_authorization`
— we stash the parsed identity in a :class:`contextvars.ContextVar` that the
ASGI dispatcher sets per request and the data-path tool handlers read.

This module is the single source of truth for the header names and the
identity shape; both the dispatcher (writer) and the bearer-token issuer
(reader, see :mod:`zelosmcp.auth.token`) import from here so they can't drift.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field

# Canonical internal header names injected by zelosgateway.
SUBJECT_HEADER = "x-zelos-subject"
SCOPES_HEADER = "x-zelos-scopes"


@dataclass(frozen=True)
class CallerIdentity:
    """The authenticated caller on whose behalf a data-path tool runs.

    ``subject`` is the OIDC ``sub``; ``scopes`` is the parsed scope list.
    An *anonymous* identity (no gateway headers present, e.g. a direct dev
    invocation) has an empty subject and no scopes — :meth:`is_authenticated`
    is then ``False`` and the token issuer refuses to mint a credential.
    """

    subject: str = ""
    scopes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_authenticated(self) -> bool:
        return bool(self.subject)

    @property
    def scope_str(self) -> str:
        return " ".join(self.scopes)


def parse_identity(subject: str | None, scopes: str | None) -> CallerIdentity:
    """Build a :class:`CallerIdentity` from raw header values.

    ``scopes`` is split on any whitespace (the OIDC ``scope`` convention is
    space-delimited; we tolerate tabs/newlines too). Empty / ``None`` inputs
    yield the anonymous identity.
    """
    subj = (subject or "").strip()
    scope_list = tuple(s for s in (scopes or "").split() if s)
    return CallerIdentity(subject=subj, scopes=scope_list)


# Carries the gateway-issued caller identity from the ASGI dispatcher into
# the data-path MCP tool handlers. Default is the anonymous identity so a
# handler invoked outside a request (unit tests) sees a well-formed, clearly
# unauthenticated value rather than ``None``.
current_identity: contextvars.ContextVar[CallerIdentity] = contextvars.ContextVar(
    "zelosmcp_current_identity",
    # CallerIdentity is frozen (immutable), so a shared default instance is
    # safe — B039 is a false positive here.
    default=CallerIdentity(),  # noqa: B039
)


def identity_from_scope_headers(headers: list[tuple[bytes, bytes]]) -> CallerIdentity:
    """Extract a :class:`CallerIdentity` from an ASGI ``scope['headers']`` list.

    Header names in ASGI scope are lowercased bytes. Missing headers yield
    the anonymous identity.
    """
    subject: str | None = None
    scopes: str | None = None
    for key, value in headers:
        name = key.decode("latin-1").lower()
        if name == SUBJECT_HEADER:
            subject = value.decode("latin-1")
        elif name == SCOPES_HEADER:
            scopes = value.decode("latin-1")
    return parse_identity(subject, scopes)
