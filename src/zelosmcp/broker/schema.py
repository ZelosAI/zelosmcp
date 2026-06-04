"""Pydantic models for the broker wire contract (zelosbroker#11).

Two surfaces:

* **Share REST** — :class:`ShareDescriptor` (and its nested
  :class:`ShareProtocol` / :class:`ProtocolAuth`) is the ``201`` body of
  ``POST /shares``.
* **Sync channel** — :class:`SyncChannel` is the ``201`` body of
  ``POST /sync/channels``; the :class:`Frame` union (discriminated by
  ``kind``) is the JSON-text WebSocket message schema relayed over the
  channel.

LLM content fields (``turn``/``token``/``tool_call``/``tool_result``/
``turn_end`` payloads) are deliberately modelled as opaque JSON — the broker
relays them verbatim and zelosmcp does not interpret them, so over-typing them
here would only couple us to LLM-provider internals.

The models are configured ``extra="allow"`` so a forward-compatible broker
that adds fields does not break deserialisation; we only pin the fields the
client and the #24 tools actually read.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

# Frame ``kind`` discriminator values, per the issue's frame schema.
KIND_OPEN = "open"
KIND_TURN = "turn"
KIND_TOKEN = "token"
KIND_TOOL_CALL = "tool_call"
KIND_TOOL_RESULT = "tool_result"
KIND_TURN_END = "turn_end"
KIND_CLOSE = "close"

# Mount-protocol kinds a share protocol can advertise.
MOUNT_WEBDAV = "webdav"
MOUNT_HTTP_FUSE = "http-fuse"


class _Wire(BaseModel):
    """Base for all wire models: forward-compatible, populated by name."""

    model_config = ConfigDict(extra="allow")


# ── Share REST models ──────────────────────────────────────────────────


class ProtocolAuth(_Wire):
    """The ``auth`` block carried by each protocol entry of a share."""

    scheme: str = "Bearer"
    token: str


class ShareProtocol(_Wire):
    """One mountable protocol the broker offers for a share."""

    kind: str  # webdav | http-fuse
    url: str
    auth: ProtocolAuth


class ShareDescriptor(_Wire):
    """``201`` body of ``POST /shares`` (zelosbroker#11)."""

    token: str
    ttl_seconds: int
    protocols: list[ShareProtocol] = Field(default_factory=list)
    mount_hint: str | None = None
    expires_at: str | None = None

    def protocol(self, kind: str) -> ShareProtocol | None:
        """Return the first protocol matching ``kind`` (e.g. ``"webdav"``)."""
        for proto in self.protocols:
            if proto.kind == kind:
                return proto
        return None

    def preferred_protocol(self) -> ShareProtocol | None:
        """Return the protocol the caller should mount first.

        Preference order is WebDAV then http-fuse then whatever the broker
        listed first — WebDAV is the EA mount path and the most broadly
        supported by IDE file pickers.
        """
        for kind in (MOUNT_WEBDAV, MOUNT_HTTP_FUSE):
            proto = self.protocol(kind)
            if proto is not None:
                return proto
        return self.protocols[0] if self.protocols else None


# ── Sync-channel handle ─────────────────────────────────────────────────


class SyncChannel(_Wire):
    """``201`` body of ``POST /sync/channels`` (zelosbroker#11)."""

    session_id: str
    attach_url: str


# ── Sync-channel frame schema ───────────────────────────────────────────
#
# JSON text WS messages, discriminated by ``kind``. All carry ``session_id``;
# everything from ``turn`` onward also carries ``turn_id``.


class _FrameBase(_Wire):
    session_id: str | None = None
    turn_id: str | None = None


class ShareMount(_Wire):
    """The ``share`` block embedded in the staged ``open`` frame."""

    token: str
    mount_protocol: str | None = None
    mount_url: str | None = None
    mount_hint: str | None = None


class OpenFrame(_FrameBase):
    kind: Literal["open"] = "open"
    subagent: str | None = None
    share: ShareMount | None = None


class TurnFrame(_FrameBase):
    """Start of a subagent turn. LLM content fields are opaque JSON."""

    kind: Literal["turn"] = "turn"


class TokenFrame(_FrameBase):
    """An incremental token delta within a turn."""

    kind: Literal["token"] = "token"
    delta: Any | None = None


class ToolCallFrame(_FrameBase):
    kind: Literal["tool_call"] = "tool_call"
    tool_call_id: str | None = None
    tool: str | None = None
    arguments: Any | None = None


class ToolResultFrame(_FrameBase):
    kind: Literal["tool_result"] = "tool_result"
    tool_call_id: str | None = None
    result: Any | None = None


class TurnEndFrame(_FrameBase):
    kind: Literal["turn_end"] = "turn_end"
    message: Any | None = None
    usage: Any | None = None


class CloseFrame(_FrameBase):
    kind: Literal["close"] = "close"
    reason: str | None = None


Frame = Annotated[
    OpenFrame | TurnFrame | TokenFrame | ToolCallFrame | ToolResultFrame | TurnEndFrame | CloseFrame,
    Field(discriminator="kind"),
]

_FRAME_ADAPTER: TypeAdapter[Any] = TypeAdapter(Frame)

# Frames that mark the end of a stream — used by #24's relay loop to stop
# receiving without waiting for the WS to close.
TERMINAL_KINDS = frozenset({KIND_TURN_END, KIND_CLOSE})


def decode_frame(raw: str | bytes) -> Any:
    """Parse one WS text message into the matching typed frame.

    Unknown ``kind`` values raise ``pydantic.ValidationError`` (the
    discriminated union has no catch-all variant) so a malformed / unexpected
    frame surfaces loudly rather than being silently dropped.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    data = json.loads(raw)
    return _FRAME_ADAPTER.validate_python(data)


def encode_frame(frame: Any) -> str:
    """Serialise a typed frame (or a plain dict) to a JSON text WS message.

    ``exclude_none=True`` keeps the wire compact and avoids sending null
    discriminator-irrelevant fields the broker doesn't expect.
    """
    if isinstance(frame, BaseModel):
        return frame.model_dump_json(exclude_none=True)
    return json.dumps(frame, default=str, separators=(",", ":"))
