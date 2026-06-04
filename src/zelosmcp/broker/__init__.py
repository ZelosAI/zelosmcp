"""Broker HTTP + WebSocket client package (#23).

Typed async client for the zelosbroker wire contract (zelosbroker#11):

* :mod:`zelosmcp.broker.schema` — Pydantic models for share descriptors,
  sync-channel handles, and the sync-channel frame schema.
* :mod:`zelosmcp.broker.client` — ``httpx.AsyncClient``-backed REST client
  driving the share lifecycle and sync-channel create/close.
* :mod:`zelosmcp.broker.sync_channel` — ``websockets`` client that attaches to
  a sync channel and (de)serialises frames.

This is the carrier substrate the sync-subagent MCP tools (#24) consume.
"""

from __future__ import annotations

from zelosmcp.broker.client import BrokerClient, BrokerError
from zelosmcp.broker.schema import (
    Frame,
    ShareDescriptor,
    SyncChannel,
    decode_frame,
    encode_frame,
)
from zelosmcp.broker.sync_channel import SyncChannelClient

__all__ = [
    "BrokerClient",
    "BrokerError",
    "ShareDescriptor",
    "SyncChannel",
    "SyncChannelClient",
    "Frame",
    "decode_frame",
    "encode_frame",
]
