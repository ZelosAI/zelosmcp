"""Tests for the broker HTTP + WebSocket client (#23).

HTTP share/sync lifecycle is mocked with ``respx``; the WebSocket sync channel
is driven through an injected fake connection (deterministic, no live server)
plus a frame-schema (de)serialisation suite.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from zelosmcp.broker.client import BrokerClient, BrokerError
from zelosmcp.broker.schema import (
    CloseFrame,
    OpenFrame,
    ShareDescriptor,
    SyncChannel,
    TokenFrame,
    ToolCallFrame,
    ToolResultFrame,
    TurnEndFrame,
    TurnFrame,
    decode_frame,
    encode_frame,
)
from zelosmcp.broker.sync_channel import SyncChannelClient, SyncChannelError

BASE = "http://broker.test"

SHARE_BODY = {
    "token": "share-tok",
    "ttl_seconds": 600,
    "protocols": [
        {
            "kind": "webdav",
            "url": "http://broker.test/dav/share-tok",
            "auth": {"scheme": "Bearer", "token": "proto-tok"},
        },
        {
            "kind": "http-fuse",
            "url": "http://broker.test/fuse/share-tok",
            "auth": {"scheme": "Bearer", "token": "fuse-tok"},
        },
    ],
    "mount_hint": "/mnt/share",
    "expires_at": "2026-01-01T00:00:00Z",
}

CHANNEL_BODY = {
    "session_id": "sess-1",
    "attach_url": "/sync/channels/sess-1/attach",
}


# ── Fake WebSocket connection ─────────────────────────────────────────────


class FakeWS:
    """Minimal websockets-protocol-shaped fake for the sync channel."""

    def __init__(self, incoming: list[str]):
        self._incoming = list(incoming)
        self.sent: list[str] = []
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        if not self._incoming:
            # Mirror websockets: raise ConnectionClosed when the peer is done.
            from websockets.exceptions import ConnectionClosedOK

            raise ConnectionClosedOK(None, None)
        return self._incoming.pop(0)

    async def close(self) -> None:
        self.closed = True


def fake_factory(frames: list[str]):
    captured: dict[str, object] = {}

    async def _connect(url: str, headers: dict[str, str]):
        captured["url"] = url
        captured["headers"] = headers
        ws = FakeWS(frames)
        captured["ws"] = ws
        return ws

    return _connect, captured


# ── Share lifecycle ───────────────────────────────────────────────────────


@respx.mock
async def test_create_share():
    route = respx.post(f"{BASE}/shares").mock(
        return_value=httpx.Response(201, json=SHARE_BODY)
    )
    async with BrokerClient(BASE, auth_header="Bearer t") as broker:
        share = await broker.create_share(
            protocols=["webdav"], workspace_path="/repo", ttl_seconds=600, caller_id="u1"
        )
    assert isinstance(share, ShareDescriptor)
    assert share.token == "share-tok"
    assert share.protocol("webdav").url.endswith("/dav/share-tok")
    assert share.preferred_protocol().kind == "webdav"
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer t"
    import json as _json

    body = _json.loads(sent.content)
    assert body == {
        "protocols": ["webdav"],
        "workspace_path": "/repo",
        "ttl_seconds": 600,
        "caller_id": "u1",
    }


@respx.mock
async def test_claim_share_idempotent_200():
    respx.post(f"{BASE}/shares/share-tok/claim").mock(
        return_value=httpx.Response(200, json={})
    )
    async with BrokerClient(BASE) as broker:
        await broker.claim_share("share-tok", "client-9")  # no raise


@respx.mock
async def test_revoke_share_204():
    route = respx.delete(f"{BASE}/shares/share-tok").mock(
        return_value=httpx.Response(204)
    )
    async with BrokerClient(BASE) as broker:
        await broker.revoke_share("share-tok", reason="done")
    assert route.calls.last.request.url.params["reason"] == "done"


@respx.mock
async def test_create_share_error_raises_brokererror():
    respx.post(f"{BASE}/shares").mock(
        return_value=httpx.Response(401, text="bad token")
    )
    async with BrokerClient(BASE) as broker:
        with pytest.raises(BrokerError) as ei:
            await broker.create_share(protocols=["webdav"])
    assert ei.value.status == 401
    assert "bad token" in ei.value.body


# ── Sync channel REST ─────────────────────────────────────────────────────


@respx.mock
async def test_open_sync_channel():
    route = respx.post(f"{BASE}/sync/channels").mock(
        return_value=httpx.Response(201, json=CHANNEL_BODY)
    )
    async with BrokerClient(BASE) as broker:
        chan = await broker.open_sync_channel(subagent="Plan", share="share-tok")
    assert isinstance(chan, SyncChannel)
    assert chan.session_id == "sess-1"
    import json as _json

    body = _json.loads(route.calls.last.request.content)
    assert body == {"subagent": "Plan", "share": "share-tok"}
    # attach URL resolves the relative path to a ws:// absolute URL.
    assert broker.attach_ws_url(chan) == "ws://broker.test/sync/channels/sess-1/attach"


@respx.mock
async def test_close_sync_channel_204():
    route = respx.post(f"{BASE}/sync/channels/sess-1/close").mock(
        return_value=httpx.Response(204)
    )
    async with BrokerClient(BASE) as broker:
        await broker.close_sync_channel("sess-1", reason="completed")
    assert route.calls.last.request.url.params["reason"] == "completed"


def test_attach_ws_url_absolute_passthrough():
    broker = BrokerClient(BASE)
    chan = SyncChannel(session_id="s", attach_url="wss://other.test/attach")
    assert broker.attach_ws_url(chan) == "wss://other.test/attach"
    chan2 = SyncChannel(session_id="s", attach_url="https://other.test/attach")
    assert broker.attach_ws_url(chan2) == "wss://other.test/attach"


@respx.mock
async def test_healthy_probe():
    respx.get(f"{BASE}/healthz").mock(return_value=httpx.Response(200, json={"ok": True}))
    async with BrokerClient(BASE) as broker:
        assert await broker.healthy() is True


@respx.mock
async def test_healthy_probe_unreachable():
    respx.get(f"{BASE}/healthz").mock(side_effect=httpx.ConnectError("boom"))
    async with BrokerClient(BASE) as broker:
        assert await broker.healthy() is False


def test_requires_base_url(monkeypatch):
    monkeypatch.delenv("ZELOS_BROKER_URL", raising=False)
    with pytest.raises(ValueError):
        BrokerClient()


# ── Sync-channel WebSocket client ─────────────────────────────────────────


async def test_sync_channel_streams_frames_to_turn_end():
    frames = [
        encode_frame(OpenFrame(session_id="s1", subagent="Plan")),
        encode_frame(TurnFrame(session_id="s1", turn_id="t1")),
        encode_frame(TokenFrame(session_id="s1", turn_id="t1", delta="he")),
        encode_frame(TokenFrame(session_id="s1", turn_id="t1", delta="llo")),
        encode_frame(TurnEndFrame(session_id="s1", turn_id="t1", message={"text": "hello"})),
        # A frame after turn_end must NOT be yielded (terminal stop).
        encode_frame(TokenFrame(session_id="s1", turn_id="t1", delta="extra")),
    ]
    factory, captured = fake_factory(frames)
    ws = SyncChannelClient("ws://broker.test/attach", auth_header="Bearer x", connect_factory=factory)
    received = []
    async with ws:
        await ws.send(TurnFrame(session_id="s1"))
        async for frame in ws.frames():
            received.append(frame)
    kinds = [f.kind for f in received]
    assert kinds == ["open", "turn", "token", "token", "turn_end"]
    # Auth header forwarded on connect.
    assert captured["headers"] == {"Authorization": "Bearer x"}
    # The turn request frame we sent was serialised.
    assert '"kind":"turn"' in captured["ws"].sent[0]
    assert captured["ws"].closed is True


async def test_sync_channel_tool_call_result_frames():
    frames = [
        encode_frame(
            ToolCallFrame(
                session_id="s1", turn_id="t1", tool_call_id="c1", tool="search", arguments={"q": "x"}
            )
        ),
        encode_frame(
            ToolResultFrame(session_id="s1", turn_id="t1", tool_call_id="c1", result={"hits": 3})
        ),
        encode_frame(CloseFrame(session_id="s1", reason="done")),
    ]
    factory, _ = fake_factory(frames)
    ws = SyncChannelClient("ws://broker.test/attach", connect_factory=factory)
    received = []
    async with ws:
        async for frame in ws.frames():
            received.append(frame)
    assert [f.kind for f in received] == ["tool_call", "tool_result", "close"]
    assert received[0].tool == "search"
    assert received[1].result == {"hits": 3}


async def test_sync_channel_recv_after_close_raises():
    factory, _ = fake_factory([])  # empty -> immediate ConnectionClosed
    ws = SyncChannelClient("ws://broker.test/attach", connect_factory=factory)
    await ws.connect()
    with pytest.raises(SyncChannelError):
        await ws.recv()


async def test_sync_channel_connect_failure_raises():
    async def bad_connect(url, headers):
        raise OSError("refused")

    ws = SyncChannelClient("ws://broker.test/attach", connect_factory=bad_connect)
    with pytest.raises(SyncChannelError):
        await ws.connect()


# ── Frame schema (de)serialisation ────────────────────────────────────────


def test_decode_open_frame_with_share():
    raw = (
        '{"kind":"open","session_id":"s1","subagent":"Plan",'
        '"share":{"token":"tk","mount_protocol":"webdav","mount_url":"http://w/d","mount_hint":"/m"}}'
    )
    frame = decode_frame(raw)
    assert isinstance(frame, OpenFrame)
    assert frame.subagent == "Plan"
    assert frame.share.token == "tk"
    assert frame.share.mount_protocol == "webdav"


def test_decode_unknown_kind_raises():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        decode_frame('{"kind":"bogus","session_id":"s1"}')


def test_encode_frame_excludes_none():
    out = encode_frame(TurnFrame(session_id="s1"))
    assert '"turn_id"' not in out  # None field dropped
    assert '"kind":"turn"' in out


def test_encode_frame_accepts_plain_dict():
    out = encode_frame({"kind": "turn", "session_id": "s1"})
    assert decode_frame(out).kind == "turn"
