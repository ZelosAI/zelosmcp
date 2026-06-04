"""WebSocket client for a broker sync channel (#23).

Wraps the ``websockets`` library to attach to a sync channel's ``attach_url``
and (de)serialise the frame schema. The broker pushes a staged ``open`` frame
on connect, then relays frames bidirectionally; this client exposes:

* :meth:`SyncChannelClient.connect` / :meth:`close` (also an async context
  manager).
* :meth:`recv` — receive and decode the next frame.
* :meth:`frames` — async-iterate frames until a terminal frame
  (``turn_end`` / ``close``) or the socket closes.
* :meth:`send` — encode and send a frame upstream (e.g. a ``turn`` request).

The connection factory is injectable (``connect_factory``) so unit tests can
substitute ``websockets``' in-process test server / a fake without a live
broker.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import websockets

from zelosmcp.broker.schema import TERMINAL_KINDS, decode_frame, encode_frame

# A connect factory takes (url, extra_headers) and returns an awaitable that
# resolves to a connected WS protocol exposing ``send`` / ``recv`` / ``close``.
ConnectFactory = Callable[[str, dict[str, str]], Awaitable[Any]]


async def _default_connect(url: str, headers: dict[str, str]) -> Any:
    """Default factory: dial ``url`` with ``websockets.connect``.

    ``additional_headers`` carries the ``Authorization`` bearer (#26) on the
    upgrade request. ``websockets`` renamed ``extra_headers`` →
    ``additional_headers`` in v13+; we target the modern name.
    """
    return await websockets.connect(url, additional_headers=headers or None)


class SyncChannelError(RuntimeError):
    """Raised on a sync-channel transport problem (connect / send / recv)."""


class SyncChannelClient:
    """Attached WebSocket sync channel.

    Construct with the ``attach_url`` (resolve it via
    :meth:`BrokerClient.attach_ws_url`) and the per-invocation bearer header,
    then :meth:`connect`. Frames received are typed models from
    :mod:`zelosmcp.broker.schema`.
    """

    def __init__(
        self,
        attach_url: str,
        *,
        auth_header: str | None = None,
        connect_factory: ConnectFactory | None = None,
    ) -> None:
        self.attach_url = attach_url
        self._headers = {"Authorization": auth_header} if auth_header else {}
        self._connect = connect_factory or _default_connect
        self._ws: Any | None = None

    async def __aenter__(self) -> SyncChannelClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def connect(self) -> None:
        if self._ws is not None:
            return
        try:
            self._ws = await self._connect(self.attach_url, self._headers)
        except Exception as exc:  # noqa: BLE001 - normalise transport errors
            raise SyncChannelError(
                f"failed to attach sync channel at {self.attach_url}: {exc}"
            ) from exc

    async def close(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is not None:
            with contextlib.suppress(Exception):  # close is best-effort
                await ws.close()

    def _require_ws(self) -> Any:
        if self._ws is None:
            raise SyncChannelError("sync channel is not connected")
        return self._ws

    async def send(self, frame: Any) -> None:
        """Encode ``frame`` (a typed frame or plain dict) and send it upstream."""
        ws = self._require_ws()
        await ws.send(encode_frame(frame))

    async def recv(self) -> Any:
        """Receive and decode the next frame.

        Raises :class:`SyncChannelError` when the socket closes before a frame
        arrives (``websockets`` raises ``ConnectionClosed`` from ``recv``).
        """
        ws = self._require_ws()
        try:
            raw = await ws.recv()
        except websockets.exceptions.ConnectionClosed as exc:
            raise SyncChannelError("sync channel closed before a frame arrived") from exc
        return decode_frame(raw)

    async def frames(self) -> AsyncIterator[Any]:
        """Yield decoded frames until a terminal frame or socket close.

        Terminal frames (``turn_end`` / ``close``, see
        :data:`zelosmcp.broker.schema.TERMINAL_KINDS`) are yielded and then end
        the iteration. A clean socket close also ends iteration without error;
        this lets the #24 relay loop simply ``async for frame in chan.frames()``.
        """
        ws = self._require_ws()
        while True:
            try:
                raw = await ws.recv()
            except websockets.exceptions.ConnectionClosed:
                return
            frame = decode_frame(raw)
            yield frame
            if getattr(frame, "kind", None) in TERMINAL_KINDS:
                return
