"""Async HTTP client for the broker share + sync-channel lifecycle (#23).

Drives the zelosbroker#11 REST surface with ``httpx.AsyncClient`` (chosen over
aiohttp to align with FastMCP / the rest of zelosmcp's async stack):

Share lifecycle
    * ``POST   /shares``                  → :class:`~zelosmcp.broker.schema.ShareDescriptor`
    * ``POST   /shares/{token}/claim``    → idempotent claim (200)
    * ``DELETE /shares/{token}?reason=``  → revoke (204)

Sync channel
    * ``POST   /sync/channels``                  → :class:`~zelosmcp.broker.schema.SyncChannel`
    * ``GET    /sync/channels/{id}/attach``      → WebSocket (handled by
      :class:`~zelosmcp.broker.sync_channel.SyncChannelClient`)
    * ``POST   /sync/channels/{id}/close?reason=`` → close (204)

The bearer token is injected by the auth layer (#26): callers pass an
``Authorization`` header value (or a per-call override) which the client
attaches to every outgoing request. This client does not mint tokens itself.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from zelosmcp.broker.schema import ShareDescriptor, SyncChannel

# Env var holding the broker base URL (e.g. ``http://zelosbroker:8080``).
BROKER_URL_ENV = "ZELOS_BROKER_URL"

_DEFAULT_TIMEOUT = 30.0


class BrokerError(RuntimeError):
    """Raised when the broker returns a non-2xx response.

    Carries the HTTP ``status`` and the raw response ``body`` so callers (and
    the #24 tools) can surface a useful message and distinguish e.g. a 401
    (bad / missing bearer token) from a 404 (unknown share).
    """

    def __init__(self, status: int, body: str, *, operation: str) -> None:
        super().__init__(f"broker {operation} failed: HTTP {status}: {body[:512]}")
        self.status = status
        self.body = body
        self.operation = operation


def _ws_url(http_url: str) -> str:
    """Map an ``http(s)://`` base URL to its ``ws(s)://`` equivalent."""
    if http_url.startswith("https://"):
        return "wss://" + http_url[len("https://") :]
    if http_url.startswith("http://"):
        return "ws://" + http_url[len("http://") :]
    return http_url


class BrokerClient:
    """Typed async client for the broker REST surface.

    Construct with an explicit ``base_url`` or let it default to
    ``$ZELOS_BROKER_URL``. ``auth_header`` is the default ``Authorization``
    header value attached to every request; individual calls may override it
    (the #24 tools issue a fresh per-invocation token via #26).

    Usable as an async context manager so the underlying ``httpx.AsyncClient``
    is closed deterministically::

        async with BrokerClient() as broker:
            share = await broker.create_share(protocols=["webdav"])
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        auth_header: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        resolved = base_url if base_url is not None else os.environ.get(BROKER_URL_ENV)
        if not resolved:
            raise ValueError(
                f"broker base_url not provided and {BROKER_URL_ENV} is unset"
            )
        self.base_url = resolved.rstrip("/")
        self.ws_base_url = _ws_url(self.base_url)
        self._auth_header = auth_header
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> BrokerClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers(self, auth_header: str | None) -> dict[str, str]:
        value = auth_header if auth_header is not None else self._auth_header
        return {"Authorization": value} if value else {}

    @staticmethod
    def _check(resp: httpx.Response, *, operation: str, expect: tuple[int, ...]) -> None:
        if resp.status_code not in expect:
            raise BrokerError(resp.status_code, resp.text, operation=operation)

    # ── Share lifecycle ────────────────────────────────────────────────

    async def create_share(
        self,
        *,
        protocols: list[str],
        workspace_path: str | None = None,
        ttl_seconds: int | None = None,
        caller_id: str | None = None,
        auth_header: str | None = None,
    ) -> ShareDescriptor:
        """``POST /shares`` → a :class:`ShareDescriptor` (201).

        ``protocols`` is the requested mount kinds (``webdav`` / ``http-fuse``);
        the broker echoes back the protocols it actually provisioned with their
        per-protocol auth.
        """
        body: dict[str, Any] = {"protocols": list(protocols)}
        if workspace_path is not None:
            body["workspace_path"] = workspace_path
        if ttl_seconds is not None:
            body["ttl_seconds"] = ttl_seconds
        if caller_id is not None:
            body["caller_id"] = caller_id
        resp = await self._client.post(
            f"{self.base_url}/shares",
            json=body,
            headers=self._headers(auth_header),
        )
        self._check(resp, operation="create_share", expect=(201,))
        return ShareDescriptor.model_validate(resp.json())

    async def claim_share(
        self,
        token: str,
        client_id: str,
        *,
        auth_header: str | None = None,
    ) -> None:
        """``POST /shares/{token}/claim`` — idempotent claim (200)."""
        resp = await self._client.post(
            f"{self.base_url}/shares/{token}/claim",
            json={"client_id": client_id},
            headers=self._headers(auth_header),
        )
        self._check(resp, operation="claim_share", expect=(200,))

    async def revoke_share(
        self,
        token: str,
        *,
        reason: str | None = None,
        auth_header: str | None = None,
    ) -> None:
        """``DELETE /shares/{token}?reason=`` — revoke (204)."""
        params = {"reason": reason} if reason is not None else None
        resp = await self._client.delete(
            f"{self.base_url}/shares/{token}",
            params=params,
            headers=self._headers(auth_header),
        )
        self._check(resp, operation="revoke_share", expect=(204,))

    # ── Sync channel ───────────────────────────────────────────────────

    async def open_sync_channel(
        self,
        *,
        target_client_id: str | None = None,
        subagent: str | None = None,
        share: str | None = None,
        auth_header: str | None = None,
    ) -> SyncChannel:
        """``POST /sync/channels`` → a :class:`SyncChannel` (201).

        ``share`` is the share *token* to bind to the channel (so the staged
        ``open`` frame can carry the mount coords). ``subagent`` is the
        subagent type the channel should drive.
        """
        body: dict[str, Any] = {}
        if target_client_id is not None:
            body["target_client_id"] = target_client_id
        if subagent is not None:
            body["subagent"] = subagent
        if share is not None:
            body["share"] = share
        resp = await self._client.post(
            f"{self.base_url}/sync/channels",
            json=body,
            headers=self._headers(auth_header),
        )
        self._check(resp, operation="open_sync_channel", expect=(201,))
        return SyncChannel.model_validate(resp.json())

    async def close_sync_channel(
        self,
        session_id: str,
        *,
        reason: str | None = None,
        auth_header: str | None = None,
    ) -> None:
        """``POST /sync/channels/{id}/close?reason=`` — close (204)."""
        params = {"reason": reason} if reason is not None else None
        resp = await self._client.post(
            f"{self.base_url}/sync/channels/{session_id}/close",
            params=params,
            headers=self._headers(auth_header),
        )
        self._check(resp, operation="close_sync_channel", expect=(204,))

    def attach_ws_url(self, channel: SyncChannel) -> str:
        """Resolve the WebSocket URL to attach to ``channel``.

        The broker may return ``attach_url`` as an absolute URL or as a path
        relative to the broker base; either way we normalise it to a
        ``ws(s)://`` absolute URL the :class:`SyncChannelClient` can dial.
        """
        url = channel.attach_url
        if url.startswith(("ws://", "wss://")):
            return url
        if url.startswith(("http://", "https://")):
            return _ws_url(url)
        # Relative path (e.g. "/sync/channels/{id}/attach").
        return f"{self.ws_base_url}/{url.lstrip('/')}"

    async def healthy(self) -> bool:
        """Best-effort reachability probe used by ``/healthz``/``/readyz``.

        Hits the broker's ``/healthz`` and returns ``True`` on a 2xx. Any
        transport error or non-2xx returns ``False`` rather than raising so a
        health route can fold this into a status payload.
        """
        try:
            resp = await self._client.get(
                f"{self.base_url}/healthz",
                headers=self._headers(None),
            )
        except httpx.HTTPError:
            return False
        return 200 <= resp.status_code < 300
