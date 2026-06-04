"""Async NATS publisher for backplane inference requests (#25).

Reuses a single ``nats-py`` connection across invocations (mirroring the
connection-management pattern of the broker client / reverse-proxy pool), and
publishes validated request envelopes onto ``inference.requests.<kind>``.

The downstream auth callback expects a bearer credential; the per-invocation
backplane token (#26) is passed in by the caller and forwarded as the NATS
connection's token. EA keeps connection-per-identity simple by binding the
token at connect time; a long-lived shared connection is reused only while the
token is unchanged.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any

from zelosmcp.backplane.envelope import (
    RequestEnvelope,
    new_reply_inbox,
    request_topic,
    validate_request_envelope,
)

# Env var holding the NATS substrate URL (e.g. ``nats://zelosbackplane:4222``).
BACKPLANE_URL_ENV = "ZELOSBACKPLANE_URL"

_DEFAULT_REQUEST_TIMEOUT = 30.0


class BackplaneError(RuntimeError):
    """Raised when the backplane cannot be reached or a publish fails."""


class BackplanePublisher:
    """Async NATS publisher with a reused connection.

    Construct with an explicit ``url`` or let it default to
    ``$ZELOSBACKPLANE_URL``. ``connect_factory`` is injectable so unit tests
    can substitute an embedded NATS server's connection (or a fake) without a
    live broker; it defaults to ``nats.connect``.
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        connect_factory: Any | None = None,
    ) -> None:
        resolved = url if url is not None else os.environ.get(BACKPLANE_URL_ENV)
        if not resolved:
            raise ValueError(
                f"backplane url not provided and {BACKPLANE_URL_ENV} is unset"
            )
        self.url = resolved
        self._connect_factory = connect_factory
        self._nc: Any | None = None
        self._token: str | None = None

    async def _connect(self, token: str | None) -> Any:
        if self._connect_factory is not None:
            return await self._connect_factory(self.url, token)
        import nats  # imported lazily so the dep is only needed at runtime

        kwargs: dict[str, Any] = {}
        if token:
            kwargs["token"] = token
        return await nats.connect(self.url, **kwargs)

    async def connect(self, *, token: str | None = None) -> Any:
        """Return a live NATS connection, opening one if needed.

        The connection is reused across calls while the bearer ``token`` is
        unchanged; a new token (per-invocation re-issue, #26) reconnects so the
        downstream auth callback sees the current credential.
        """
        if self._nc is not None and self._token == token:
            if getattr(self._nc, "is_closed", False):
                self._nc = None
            else:
                return self._nc
        if self._nc is not None:
            await self.close()
        try:
            self._nc = await self._connect(token)
        except Exception as exc:  # noqa: BLE001 - normalise connect errors
            raise BackplaneError(
                f"failed to connect to backplane at {self.url}: {exc}"
            ) from exc
        self._token = token
        return self._nc

    async def close(self) -> None:
        nc = self._nc
        self._nc = None
        self._token = None
        if nc is not None:
            # Best-effort graceful drain; fall back to a hard close, and
            # ignore failures in either path (we're tearing down anyway).
            try:
                await nc.drain()
            except Exception:  # noqa: BLE001 - close is best-effort
                with contextlib.suppress(Exception):
                    await nc.close()

    async def publish_request(
        self,
        envelope: RequestEnvelope,
        *,
        token: str | None = None,
    ) -> dict[str, str]:
        """Validate and publish ``envelope`` onto ``inference.requests.<kind>``.

        Returns ``{"id", "replyTopic"}`` — the envelope id (the correlation id
        the response echoes as ``corrId``) and the reply topic the caller
        should subscribe to for the response. If the envelope has no
        ``replyTo``, a fresh per-request inbox is assigned before publishing.

        Raises :class:`BackplaneError` on a transport failure and
        ``jsonschema.ValidationError`` if the envelope does not match the
        canonical request schema.
        """
        if envelope.replyTo is None:
            envelope.replyTo = new_reply_inbox()
        validate_request_envelope(envelope)
        nc = await self.connect(token=token)
        subject = request_topic(envelope.kind)
        try:
            await nc.publish(
                subject,
                envelope.to_json(),
                reply=envelope.replyTo,
            )
            await nc.flush()
        except Exception as exc:  # noqa: BLE001 - normalise publish errors
            raise BackplaneError(
                f"failed to publish to {subject}: {exc}"
            ) from exc
        return {"id": envelope.id, "replyTopic": envelope.replyTo}

    async def request_reply(
        self,
        envelope: RequestEnvelope,
        *,
        token: str | None = None,
        timeout: float = _DEFAULT_REQUEST_TIMEOUT,
    ) -> bytes:
        """Publish ``envelope`` and await the correlated reply (round-trip).

        Uses NATS request/reply (the reply lands on the envelope's per-request
        inbox). Returns the raw reply payload bytes. Raises
        :class:`BackplaneError` on timeout / transport failure. Primarily used
        by the end-to-end verification path; the MCP tool itself returns
        immediately with ``{id, replyTopic}``.
        """
        if envelope.replyTo is None:
            envelope.replyTo = new_reply_inbox()
        validate_request_envelope(envelope)
        nc = await self.connect(token=token)
        subject = request_topic(envelope.kind)
        try:
            msg = await nc.request(subject, envelope.to_json(), timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            raise BackplaneError(
                f"request to {subject} failed: {exc}"
            ) from exc
        return msg.data
