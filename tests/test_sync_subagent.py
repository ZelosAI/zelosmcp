"""Tests for the sync-subagent MCP tools + data-path server (#24).

Drives ``run_sync_subagent`` against a fake broker (respx for REST + an
injected fake WS) and asserts the full frame sequence
(open → turn → token+ → tool_call/tool_result → turn_end) is relayed, the
consolidated transcript is returned, and the share + channel are torn down.
Also covers the data-path MCP server's tool registration + dispatch.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from zelosmcp.auth.identity import CallerIdentity
from zelosmcp.broker.client import BrokerClient
from zelosmcp.broker.schema import (
    OpenFrame,
    TokenFrame,
    ToolCallFrame,
    ToolResultFrame,
    TurnEndFrame,
    TurnFrame,
    encode_frame,
)
from zelosmcp.loader import get_subagent, list_subagents
from zelosmcp.server import DataPathServer, build_tools
from zelosmcp.tools.sync_subagent import (
    SubagentDeps,
    run_sync_subagent,
    subagent_tool_specs,
)

KEY = "test-signing-key-which-is-32-bytes!!"
BASE = "http://broker.test"

SHARE_BODY = {
    "token": "share-1",
    "ttl_seconds": 600,
    "protocols": [
        {"kind": "webdav", "url": "http://broker.test/dav/share-1", "auth": {"scheme": "Bearer", "token": "p"}}
    ],
    "mount_hint": "/mnt/1",
    "expires_at": "2026-01-01T00:00:00Z",
}
CHANNEL_BODY = {"session_id": "sess-9", "attach_url": "/sync/channels/sess-9/attach"}


class FakeWS:
    def __init__(self, incoming):
        self._incoming = list(incoming)
        self.sent = []
        self.closed = False

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        if not self._incoming:
            from websockets.exceptions import ConnectionClosedOK

            raise ConnectionClosedOK(None, None)
        return self._incoming.pop(0)

    async def close(self):
        self.closed = True


def channel_factory(frames):
    box = {}

    def _factory(attach_url, auth_header):
        from zelosmcp.broker.sync_channel import SyncChannelClient

        async def _connect(url, headers):
            ws = FakeWS(frames)
            box["ws"] = ws
            box["url"] = url
            box["headers"] = headers
            return ws

        return SyncChannelClient(attach_url, auth_header=auth_header, connect_factory=_connect)

    return _factory, box


def full_turn_frames():
    return [
        encode_frame(OpenFrame(session_id="sess-9", subagent="Plan")),
        encode_frame(TurnFrame(session_id="sess-9", turn_id="t1")),
        encode_frame(TokenFrame(session_id="sess-9", turn_id="t1", delta="Step 1. ")),
        encode_frame(TokenFrame(session_id="sess-9", turn_id="t1", delta="Step 2.")),
        encode_frame(
            ToolCallFrame(session_id="sess-9", turn_id="t1", tool_call_id="c1", tool="read", arguments={"f": "x"})
        ),
        encode_frame(
            ToolResultFrame(session_id="sess-9", turn_id="t1", tool_call_id="c1", result={"text": "..."})
        ),
        encode_frame(
            TurnEndFrame(
                session_id="sess-9",
                turn_id="t1",
                message={"text": "Step 1. Step 2."},
                usage={"input_tokens": 12, "output_tokens": 8},
            )
        ),
    ]


# ── Roster ────────────────────────────────────────────────────────────────


def test_subagent_roster():
    names = [m.name for m in list_subagents()]
    assert names == ["Plan", "Explore", "general-purpose"]
    assert get_subagent("plan").name == "Plan"
    assert get_subagent("general_purpose").name == "general-purpose"
    assert get_subagent("nope") is None


def test_subagent_tool_specs_match_roster():
    assert [m.tool_name for m in subagent_tool_specs()] == [
        "plan",
        "explore",
        "general_purpose",
    ]


# ── run_sync_subagent ───────────────────────────────────────────────────────


@respx.mock
async def test_run_sync_subagent_full_sequence():
    create_share = respx.post(f"{BASE}/shares").mock(
        return_value=httpx.Response(201, json=SHARE_BODY)
    )
    open_chan = respx.post(f"{BASE}/sync/channels").mock(
        return_value=httpx.Response(201, json=CHANNEL_BODY)
    )
    close_chan = respx.post(f"{BASE}/sync/channels/sess-9/close").mock(
        return_value=httpx.Response(204)
    )
    revoke = respx.delete(f"{BASE}/shares/share-1").mock(return_value=httpx.Response(204))

    factory, box = channel_factory(full_turn_frames())
    streamed = []

    async def sink(frame):
        streamed.append(frame.kind)

    meta = get_subagent("plan")
    ident = CallerIdentity(subject="dev-1", scopes=("plan",))
    async with BrokerClient(BASE) as broker:
        deps = SubagentDeps(
            broker=broker,
            channel_factory=factory,
            identity=ident,
            signing_key=KEY,
            frame_sink=sink,
        )
        result = await run_sync_subagent(
            meta, prompt="plan the work", deps=deps, workspace_path="/repo"
        )

    # Complete frame sequence relayed.
    kinds = [f["kind"] for f in result.frames]
    assert kinds == ["open", "turn", "token", "token", "tool_call", "tool_result", "turn_end"]
    assert streamed == kinds
    # Consolidated transcript + usage from turn_end.
    assert result.transcript == {"text": "Step 1. Step 2."}
    assert result.usage == {"input_tokens": 12, "output_tokens": 8}
    assert result.session_id == "sess-9"
    assert result.subagent == "Plan"

    # Lifecycle: share created, channel opened, then both torn down.
    assert create_share.called and open_chan.called
    assert close_chan.called and revoke.called

    # Open-channel request carried the subagent + share token.
    body = json.loads(open_chan.calls.last.request.content)
    assert body == {"subagent": "Plan", "share": "share-1"}

    # A broker bearer token was issued for the caller and forwarded.
    auth = create_share.calls.last.request.headers["authorization"]
    assert auth.startswith("Bearer ")
    from zelosmcp.auth.token import AUDIENCE_BROKER, verify_token

    claims = verify_token(auth.split(" ", 1)[1], AUDIENCE_BROKER, signing_key=KEY)
    assert claims["sub"] == "dev-1"

    # The turn request frame was sent upstream first.
    assert '"kind":"turn"' in box["ws"].sent[0]
    assert box["ws"].closed is True


@respx.mock
async def test_run_sync_subagent_without_share():
    respx.post(f"{BASE}/sync/channels").mock(return_value=httpx.Response(201, json=CHANNEL_BODY))
    respx.post(f"{BASE}/sync/channels/sess-9/close").mock(return_value=httpx.Response(204))
    # No /shares route registered -> must not be called.
    frames = [
        encode_frame(OpenFrame(session_id="sess-9")),
        encode_frame(TurnEndFrame(session_id="sess-9", turn_id="t1", message={"text": "ok"})),
    ]
    factory, _ = channel_factory(frames)
    meta = get_subagent("explore")
    ident = CallerIdentity(subject="dev-2")
    async with BrokerClient(BASE) as broker:
        deps = SubagentDeps(
            broker=broker, channel_factory=factory, identity=ident, signing_key=KEY
        )
        result = await run_sync_subagent(
            meta, prompt="explore", deps=deps, open_share=False
        )
    assert result.transcript == {"text": "ok"}


@respx.mock
async def test_run_sync_subagent_closes_channel_on_relay_error():
    respx.post(f"{BASE}/sync/channels").mock(return_value=httpx.Response(201, json=CHANNEL_BODY))
    close_chan = respx.post(f"{BASE}/sync/channels/sess-9/close").mock(
        return_value=httpx.Response(204)
    )

    # A WS that raises mid-stream on recv.
    class ExplodingWS(FakeWS):
        async def recv(self):
            raise RuntimeError("ws blew up")

    def factory(attach_url, auth_header):
        from zelosmcp.broker.sync_channel import SyncChannelClient

        async def _connect(url, headers):
            return ExplodingWS([])

        return SyncChannelClient(attach_url, auth_header=auth_header, connect_factory=_connect)

    meta = get_subagent("plan")
    ident = CallerIdentity(subject="dev-3")
    async with BrokerClient(BASE) as broker:
        deps = SubagentDeps(broker=broker, channel_factory=factory, identity=ident, signing_key=KEY)
        with pytest.raises(RuntimeError):
            await run_sync_subagent(meta, prompt="x", deps=deps, open_share=False)
    # Channel still closed despite the relay error.
    assert close_chan.called


# ── DataPathServer ──────────────────────────────────────────────────────────


def test_data_path_server_tool_registration():
    tools = build_tools()
    names = [t.name for t in tools]
    assert names == ["plan", "explore", "general_purpose", "submit_inference_task"]
    # Subagent tools take prompt; required.
    plan = next(t for t in tools if t.name == "plan")
    assert plan.inputSchema["required"] == ["prompt"]
    submit = next(t for t in tools if t.name == "submit_inference_task")
    assert "prompt" in submit.inputSchema["properties"]


async def test_data_path_server_dispatch_unknown_tool_raises():
    from mcp.shared.exceptions import McpError

    class _Mgr:
        pass

    srv = DataPathServer(_Mgr())
    with pytest.raises(McpError):
        await srv._dispatch("nonexistent", {})


async def test_data_path_server_missing_broker_env_errors(monkeypatch):
    from mcp.shared.exceptions import McpError

    monkeypatch.delenv("ZELOS_BROKER_URL", raising=False)

    class _Mgr:
        pass

    srv = DataPathServer(_Mgr())
    with pytest.raises(McpError):
        await srv._call_subagent(get_subagent("plan"), {"prompt": "hi"})


async def test_data_path_server_excluded_from_aggregator_surface():
    # client_session is None so the /mcp aggregator + /api/catalog skip it.
    class _Mgr:
        pass

    srv = DataPathServer(_Mgr())
    assert srv.client_session is None
    assert srv.name == "zelos"
