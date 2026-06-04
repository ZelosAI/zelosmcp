"""Unit tests for zelosmcp.manager.ProxyManager."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from zelosmcp.manager import ProxyManager
from tests.conftest import (
    fake_stdio_client,
    fake_sse_client,
    fake_http_client,
    make_mock_session,
)


def _patches():
    mock_session = make_mock_session()

    @asynccontextmanager
    async def patched_client_session(read, write):
        yield mock_session

    @asynccontextmanager
    async def patched_run(self):
        yield

    return [
        patch("zelosmcp.proxy.stdio_client", side_effect=fake_stdio_client),
        patch("zelosmcp.proxy.sse_client", side_effect=fake_sse_client),
        patch("zelosmcp.proxy.streamablehttp_client", side_effect=fake_http_client),
        patch("zelosmcp.proxy.ClientSession", side_effect=patched_client_session),
        patch("zelosmcp.proxy.StreamableHTTPSessionManager.run", patched_run),
    ]


_CONFIG = {
    "mcpServers": {
        "alpha": {"command": "echo", "args": ["a"]},
        "beta":  {"type": "sse", "url": "http://x/sse"},
        "gamma": {"type": "streamable-http", "url": "http://x/mcp"},
    },
}


class TestStartAll:
    @pytest.mark.asyncio
    async def test_starts_every_server(self):
        with _patches()[0], _patches()[1], _patches()[2], _patches()[3], _patches()[4]:
            m = ProxyManager(mandatory_config_path="")
            result = await m.start_all(_CONFIG)
            assert result["primary"] is None
            assert set(result["servers"].keys()) == {"alpha", "beta", "gamma"}
            assert all(r["ok"] for r in result["servers"].values())
            assert m.primary is None
            assert m.aggregator.running is True
            for name in ("alpha", "beta", "gamma"):
                assert m.get(name).running is True
            await m.stop_all()
            assert m.aggregator.running is False

    @pytest.mark.asyncio
    async def test_primarymcp_is_deprecated_but_accepted(self):
        with _patches()[0], _patches()[1], _patches()[2], _patches()[3], _patches()[4]:
            m = ProxyManager(mandatory_config_path="")
            q = m.subscribe_logs()
            await m.start_all({
                "primaryMCP": "alpha",
                "mcpServers": {"alpha": {"command": "echo"}},
            })
            assert m.primary is None
            seen = []
            try:
                while True:
                    seen.append(q.get_nowait())
            except asyncio.QueueEmpty:
                pass
            assert any("primaryMCP is deprecated" in line for line in seen)
            await m.stop_all()
            m.unsubscribe_logs(q)

    @pytest.mark.asyncio
    async def test_applies_event_settings_from_config(self):
        with _patches()[0], _patches()[1], _patches()[2], _patches()[3], _patches()[4]:
            m = ProxyManager(mandatory_config_path="")
            await m.start_all({
                "event_retention_hours": 24,
                "event_prune_interval_mins": 5,
                "mcpServers": {"alpha": {"command": "echo"}},
            })
            assert m.event_retention_hours == 24
            assert m.event_prune_interval_mins == 5
            await m.stop_all()

    @pytest.mark.asyncio
    async def test_replaces_existing_servers(self):
        with _patches()[0], _patches()[1], _patches()[2], _patches()[3], _patches()[4]:
            m = ProxyManager(mandatory_config_path="")
            await m.start_all(_CONFIG)
            await m.start_all({"mcpServers": {"only": {"command": "echo"}}})
            # `zelosmcp` (introspection builtin) and `zelos` (data-path
            # server) are always-on built-ins; they live alongside any
            # user-configured backends and survive start_all/stop_all.
            user_names = [n for n in m.names() if n not in ("zelosmcp", "zelos")]
            assert user_names == ["only"]
            assert m.primary is None
            await m.stop_all()

    @pytest.mark.asyncio
    async def test_failure_in_one_does_not_stop_others(self):
        good = make_mock_session()

        @asynccontextmanager
        async def patched_client_session(read, write):
            yield good

        @asynccontextmanager
        async def patched_run(self):
            yield

        @asynccontextmanager
        async def failing_sse(url, *a, **kw):
            raise ConnectionError("sse boom")
            yield  # pragma: no cover

        with (
            patch("zelosmcp.proxy.stdio_client", side_effect=fake_stdio_client),
            patch("zelosmcp.proxy.sse_client", side_effect=failing_sse),
            patch("zelosmcp.proxy.ClientSession", side_effect=patched_client_session),
            patch("zelosmcp.proxy.StreamableHTTPSessionManager.run", patched_run),
        ):
            m = ProxyManager(mandatory_config_path="")
            result = await m.start_all({
                "mcpServers": {
                    "ok":  {"command": "echo"},
                    "bad": {"type": "sse", "url": "http://x/sse"},
                }
            })
            assert result["servers"]["ok"]["ok"] is True
            assert result["servers"]["bad"]["ok"] is False
            assert "boom" in result["servers"]["bad"]["error"]
            assert m.get("ok").running is True
            assert m.get("bad").running is False
            await m.stop_all()


class TestPerServer:
    @pytest.mark.asyncio
    async def test_stop_one_then_start_one(self):
        with _patches()[0], _patches()[1], _patches()[2], _patches()[3], _patches()[4]:
            m = ProxyManager(mandatory_config_path="")
            await m.start_all(_CONFIG)
            await m.stop_one("beta")
            assert m.get("beta").running is False
            await m.start_one("beta")
            assert m.get("beta").running is True
            await m.stop_all()

    @pytest.mark.asyncio
    async def test_started_false_skips_startup(self):
        with _patches()[0], _patches()[1], _patches()[2], _patches()[3], _patches()[4]:
            m = ProxyManager(mandatory_config_path="")
            result = await m.start_all({
                "mcpServers": {
                    "running": {"command": "echo"},
                    "stopped": {
                        "command": "echo",
                        "started": False,
                    },
                },
            })
            # "stopped" is registered but not running.
            assert result["servers"]["running"]["ok"] is True
            assert result["servers"]["stopped"]["ok"] is True
            assert result["servers"]["stopped"]["started"] is False
            assert m.get("running").running is True
            assert m.get("stopped").running is False
            # Spec is still available (for assets etc.).
            assert m.get_spec("stopped") is not None
            # Can be started later.
            await m.start_one("stopped")
            assert m.get("stopped").running is True
            await m.stop_all()

    @pytest.mark.asyncio
    async def test_unknown_name_raises(self):
        m = ProxyManager(mandatory_config_path="")
        with pytest.raises(KeyError):
            await m.start_one("ghost")
        with pytest.raises(KeyError):
            await m.stop_one("ghost")

    @pytest.mark.asyncio
    async def test_start_one_when_already_running_raises(self):
        with _patches()[0], _patches()[1], _patches()[2], _patches()[3], _patches()[4]:
            m = ProxyManager(mandatory_config_path="")
            await m.start_all(_CONFIG)
            with pytest.raises(RuntimeError, match="already running"):
                await m.start_one("alpha")
            await m.stop_all()


class TestEventPruning:
    @pytest.mark.asyncio
    async def test_prune_events_once_deletes_old_rows_and_logs(self, monkeypatch):
        m = ProxyManager(mandatory_config_path="")
        monkeypatch.setattr("zelosmcp.manager.time.time", lambda: 1000.0)
        await m.start_savings(":memory:")
        assert m.events is not None
        await m.events.record_event(
            event_id="old-event",
            method="tools/call",
            backend="docker",
            tool="list_containers",
            qualified="docker__list_containers",
            compressed=False,
            input_payload={"all": True},
            output_payload={"count": 1},
        )
        q = m.subscribe_logs()
        m._event_retention_hours = 1
        monkeypatch.setattr("zelosmcp.manager.time.time", lambda: 8200.0)

        deleted = await m._prune_events_once()

        assert deleted == 1
        seen = []
        try:
            while True:
                seen.append(q.get_nowait())
        except asyncio.QueueEmpty:
            pass
        assert any("Pruned 1 events older than 1h" in line for line in seen)
        await m.stop_savings()


class TestStatus:
    @pytest.mark.asyncio
    async def test_status_when_empty(self):
        m = ProxyManager(mandatory_config_path="")
        s = m.status()
        # `zelosmcp` (introspection builtin) and `zelos` (data-path server)
        # are always-on built-ins; until their lifespan-managed start hooks
        # have run, `state.running` is still False, but the rows exist so the
        # UI can render the slots.
        assert s["primary"] is None
        assert s["running"] is False
        by_name = {row["name"]: row for row in s["servers"]}
        assert set(by_name) == {"zelosmcp", "zelos"}
        assert by_name["zelosmcp"]["builtin"] is True
        assert by_name["zelosmcp"]["running"] is False
        assert by_name["zelos"]["builtin"] is True
        assert by_name["zelos"]["running"] is False

    @pytest.mark.asyncio
    async def test_status_running(self):
        with _patches()[0], _patches()[1], _patches()[2], _patches()[3], _patches()[4]:
            m = ProxyManager(mandatory_config_path="")
            await m.start_all(_CONFIG)
            s = m.status()
            assert s["primary"] is None
            assert s["running"] is True
            by_name = {srv["name"]: srv for srv in s["servers"]}
            for name in ("alpha", "beta", "gamma"):
                assert by_name[name]["primary"] is False
            assert by_name["alpha"]["transport"] == "stdio"
            assert by_name["beta"]["transport"] == "sse"
            assert by_name["gamma"]["transport"] == "http"
            await m.stop_all()


class TestLogAggregation:
    @pytest.mark.asyncio
    async def test_logs_from_children_are_broadcast(self):
        with _patches()[0], _patches()[1], _patches()[2], _patches()[3], _patches()[4]:
            m = ProxyManager(mandatory_config_path="")
            q = m.subscribe_logs()
            await m.start_all({"mcpServers": {"alpha": {"command": "echo"}}})

            # Drain a few messages, looking for the [alpha] tag.
            seen = []
            try:
                while True:
                    msg = await asyncio.wait_for(q.get(), timeout=0.1)
                    seen.append(msg)
            except asyncio.TimeoutError:
                pass
            assert any("[alpha]" in s for s in seen)
            await m.stop_all()
            m.unsubscribe_logs(q)

    def test_unsubscribe_unknown_is_noop(self):
        m = ProxyManager(mandatory_config_path="")
        q = asyncio.Queue()
        m.unsubscribe_logs(q)


class TestReverseProxy:
    """Lookup behavior of ProxyManager.find_reverse_proxy.

    The httpx client and proxy_request integration is exercised via the
    end-to-end tests in test_app_integration.py. This block only covers
    the path-matching / longest-prefix logic.
    """

    @pytest.mark.asyncio
    async def test_find_returns_running_state(self):
        with _patches()[0], _patches()[1], _patches()[2], _patches()[3], _patches()[4]:
            m = ProxyManager(mandatory_config_path="")
            await m.start_all({
                "mcpServers": {
                    "alpha": {
                        "command": "echo",
                        "reverseProxy": {
                            "mount": "/alpha",
                            "upstream": "http://127.0.0.1:9000",
                        },
                    },
                },
            })
            match = m.find_reverse_proxy("/alpha/v1/health")
            assert match is not None
            spec, state = match
            assert spec.name == "alpha"
            assert state is m.get("alpha")
            assert state.running is True
            await m.stop_all()

    @pytest.mark.asyncio
    async def test_find_returns_none_for_no_match(self):
        with _patches()[0], _patches()[1], _patches()[2], _patches()[3], _patches()[4]:
            m = ProxyManager(mandatory_config_path="")
            await m.start_all({
                "mcpServers": {
                    "alpha": {
                        "command": "echo",
                        "reverseProxy": {
                            "mount": "/alpha",
                            "upstream": "http://127.0.0.1:9000",
                        },
                    },
                },
            })
            assert m.find_reverse_proxy("/beta/v1/health") is None
            assert m.find_reverse_proxy("/api/status") is None
            await m.stop_all()

    @pytest.mark.asyncio
    async def test_find_skips_backends_without_proxy(self):
        with _patches()[0], _patches()[1], _patches()[2], _patches()[3], _patches()[4]:
            m = ProxyManager(mandatory_config_path="")
            await m.start_all({
                "mcpServers": {
                    "alpha": {"command": "echo"},
                },
            })
            assert m.find_reverse_proxy("/alpha/x") is None
            await m.stop_all()

    @pytest.mark.asyncio
    async def test_segment_aware_no_false_positive(self):
        """``/foo`` must not match ``/foobar/...``."""
        with _patches()[0], _patches()[1], _patches()[2], _patches()[3], _patches()[4]:
            m = ProxyManager(mandatory_config_path="")
            await m.start_all({
                "mcpServers": {
                    "foo": {
                        "command": "echo",
                        "reverseProxy": {
                            "mount": "/foo",
                            "upstream": "http://127.0.0.1:9000",
                        },
                    },
                },
            })
            assert m.find_reverse_proxy("/foobar/v1") is None
            assert m.find_reverse_proxy("/foo") is not None
            assert m.find_reverse_proxy("/foo/anything") is not None
            await m.stop_all()

    @pytest.mark.asyncio
    async def test_status_includes_reverse_proxy(self):
        with _patches()[0], _patches()[1], _patches()[2], _patches()[3], _patches()[4]:
            m = ProxyManager(mandatory_config_path="")
            await m.start_all({
                "mcpServers": {
                    "alpha": {
                        "command": "echo",
                        "reverseProxy": {
                            "mount": "/alpha",
                            "upstream": "http://127.0.0.1:9000",
                        },
                    },
                },
            })
            status = m.status()
            row = next(s for s in status["servers"] if s["name"] == "alpha")
            assert row["spec"]["reverseProxy"] == {
                "mount": "/alpha",
                "upstream": "http://127.0.0.1:9000",
            }
            await m.stop_all()

    @pytest.mark.asyncio
    async def test_http_client_lifecycle_idempotent(self):
        m = ProxyManager(mandatory_config_path="")
        # Idempotent: calling start twice doesn't reinitialise.
        await m.start_http_client()
        first = m._http_client
        await m.start_http_client()
        assert m._http_client is first
        # Idempotent: calling stop twice is fine.
        await m.stop_http_client()
        assert m._http_client is None
        await m.stop_http_client()
        assert m._http_client is None


# ── proxy_mcp_request (Phase 1C streaming passthrough) ─────────────────


class _RecorderSend:
    """ASGI ``send`` collector. Captures ``http.response.start`` (status +
    headers) plus all ``http.response.body`` chunks so tests can assert on
    streaming + header pass-through behaviour.
    """

    def __init__(self) -> None:
        self.status: int | None = None
        self.headers: list[tuple[bytes, bytes]] = []
        self.chunks: list[bytes] = []
        self.completed: bool = False

    async def __call__(self, message: dict) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = list(message["headers"])
        elif message["type"] == "http.response.body":
            chunk = message.get("body", b"") or b""
            if chunk:
                self.chunks.append(chunk)
            if not message.get("more_body", False):
                self.completed = True

    @property
    def body(self) -> bytes:
        return b"".join(self.chunks)

    def header_value(self, name: str) -> str | None:
        target = name.lower().encode("latin-1")
        for raw_name, raw_value in self.headers:
            if raw_name.lower() == target:
                return raw_value.decode("latin-1")
        return None


def _make_receive(body: bytes):
    """ASGI ``receive`` factory that yields a single body message."""
    sent = False

    async def receive() -> dict:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        # Subsequent reads block forever in real ASGI; the manager code
        # never reads past more_body=False, so an unreachable sleep is
        # the safest "shouldn't happen" stub.
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    return receive


class _StreamRecorder:
    """Captures a single httpx request and serves a canned response.

    Used as a stand-in for ``httpx.AsyncClient`` so we can assert on the
    exact outbound request without ever opening a real connection.
    """

    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.chunks = chunks or [b""]
        # Last captured request (one per recorder is enough for tests).
        self.captured_method: str | None = None
        self.captured_url: str | None = None
        self.captured_headers: list[tuple[str, str]] = []
        self.captured_content: bytes | None = None

    def build_request(
        self,
        method: str,
        url: str,
        *,
        headers: list[tuple[str, str]] | None = None,
        content: bytes | None = None,
    ):
        # Used only as a quick validity check by the production code; we
        # don't need to fully emulate httpx.Request here.
        class _R:
            pass

        return _R()

    def stream(
        self,
        method: str,
        url: str,
        *,
        headers: list[tuple[str, str]] | None = None,
        content: bytes | None = None,
    ):
        """Mirrors ``httpx.AsyncClient.stream`` — returns an async CM."""
        self.captured_method = method
        self.captured_url = url
        self.captured_headers = list(headers or [])
        self.captured_content = content

        recorder = self
        chunks = list(self.chunks)

        class _Resp:
            def __init__(self):
                self.status_code = recorder.status_code

                class _H:
                    @property
                    def raw(self_inner):
                        return [
                            (k.encode("latin-1"), v.encode("latin-1"))
                            for k, v in recorder.headers.items()
                        ]

                self.headers = _H()

            async def aiter_raw(self):
                for c in chunks:
                    yield c

        @asynccontextmanager
        async def cm():
            yield _Resp()

        return cm()


class TestProxyMcpRequest:
    """Unit-level coverage for the streaming MCP passthrough forwarder."""

    @staticmethod
    def _scope(
        *,
        path: str = "/github/mcp",
        method: str = "POST",
        headers: list[tuple[bytes, bytes]] | None = None,
    ) -> dict:
        return {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode("latin-1"),
            "query_string": b"",
            "scheme": "http",
            "client": ("127.0.0.1", 12345),
            "headers": headers or [],
        }

    @staticmethod
    def _spec(
        *,
        url: str = "https://api.example.com/mcp",
        auth_bearer: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        from zelosmcp.config import ServerSpec

        return ServerSpec(
            name="github",
            transport="http",
            url=url,
            headers=headers,
            passthrough=True,
            auth_bearer=auth_bearer,
        )

    @pytest.mark.asyncio
    async def test_forwards_authorization_verbatim(self):
        m = ProxyManager(mandatory_config_path="")
        recorder = _StreamRecorder(
            status_code=200,
            headers={"content-type": "application/json"},
            chunks=[b'{"ok":true}'],
        )
        m._http_client = recorder  # type: ignore[assignment]

        sender = _RecorderSend()
        scope = self._scope(headers=[
            (b"authorization", b"Bearer client-token-123"),
            (b"content-type", b"application/json"),
            (b"host", b"localhost:8000"),
        ])
        await m.proxy_mcp_request(
            self._spec(auth_bearer="static-fallback"),
            scope,
            _make_receive(b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'),
            sender,
        )

        assert sender.status == 200
        assert sender.body == b'{"ok":true}'
        # Inbound Authorization wins — fallback bearer must NOT be
        # injected when the caller already supplied one.
        sent_auth = [v for k, v in recorder.captured_headers if k.lower() == "authorization"]
        assert sent_auth == ["Bearer client-token-123"]
        # Host stripped so httpx synthesises the upstream's host.
        assert all(k.lower() != "host" for k, _ in recorder.captured_headers)
        # URL forwarded to the configured upstream MCP endpoint, not the
        # inbound /github/mcp path.
        assert recorder.captured_url == "https://api.example.com/mcp"
        assert recorder.captured_content == b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

    @pytest.mark.asyncio
    async def test_static_bearer_fallback_when_no_inbound_auth(self):
        m = ProxyManager(mandatory_config_path="")
        recorder = _StreamRecorder(status_code=200, headers={}, chunks=[b"{}"])
        m._http_client = recorder  # type: ignore[assignment]

        sender = _RecorderSend()
        scope = self._scope(headers=[(b"content-type", b"application/json")])
        await m.proxy_mcp_request(
            self._spec(auth_bearer="static-token"),
            scope,
            _make_receive(b"{}"),
            sender,
        )
        sent_auth = [v for k, v in recorder.captured_headers if k.lower() == "authorization"]
        assert sent_auth == ["Bearer static-token"]

    @pytest.mark.asyncio
    async def test_no_authorization_when_neither_inbound_nor_static(self):
        m = ProxyManager(mandatory_config_path="")
        recorder = _StreamRecorder(status_code=401, headers={}, chunks=[b""])
        m._http_client = recorder  # type: ignore[assignment]

        sender = _RecorderSend()
        scope = self._scope(headers=[])
        await m.proxy_mcp_request(self._spec(), scope, _make_receive(b"{}"), sender)
        sent_auth = [v for k, v in recorder.captured_headers if k.lower() == "authorization"]
        assert sent_auth == []  # Nothing injected; upstream gets to challenge.

    @pytest.mark.asyncio
    async def test_propagates_401_and_www_authenticate(self):
        m = ProxyManager(mandatory_config_path="")
        recorder = _StreamRecorder(
            status_code=401,
            headers={
                "www-authenticate": (
                    'Bearer resource_metadata='
                    '"https://api.example.com/.well-known/oauth-protected-resource"'
                ),
                "content-type": "application/json",
            },
            chunks=[b'{"error":"unauthorized"}'],
        )
        m._http_client = recorder  # type: ignore[assignment]

        sender = _RecorderSend()
        await m.proxy_mcp_request(
            self._spec(),
            self._scope(headers=[]),
            _make_receive(b"{}"),
            sender,
        )

        assert sender.status == 401
        # WWW-Authenticate must be forwarded verbatim so the client's
        # OAuth handler can fetch resource metadata directly upstream.
        ww = sender.header_value("www-authenticate")
        assert ww is not None
        assert "resource_metadata" in ww
        assert "api.example.com" in ww
        assert sender.body == b'{"error":"unauthorized"}'

    @pytest.mark.asyncio
    async def test_streams_multi_chunk_response(self):
        m = ProxyManager(mandatory_config_path="")
        recorder = _StreamRecorder(
            status_code=200,
            headers={"content-type": "text/event-stream"},
            chunks=[b"event: message\n", b'data: {"ok":1}\n\n', b'data: {"done":1}\n\n'],
        )
        m._http_client = recorder  # type: ignore[assignment]

        sender = _RecorderSend()
        await m.proxy_mcp_request(
            self._spec(),
            self._scope(headers=[(b"accept", b"text/event-stream")]),
            _make_receive(b"{}"),
            sender,
        )
        assert sender.completed is True
        assert sender.status == 200
        assert sender.body == (
            b"event: message\n"
            b'data: {"ok":1}\n\n'
            b'data: {"done":1}\n\n'
        )
        # Chunks are emitted as separate ASGI body messages so the
        # client gets to consume each frame as it arrives.
        assert len(sender.chunks) == 3

    @pytest.mark.asyncio
    async def test_strips_hop_by_hop_request_headers(self):
        m = ProxyManager(mandatory_config_path="")
        recorder = _StreamRecorder(status_code=200, headers={}, chunks=[b"{}"])
        m._http_client = recorder  # type: ignore[assignment]

        sender = _RecorderSend()
        scope = self._scope(headers=[
            (b"connection", b"close"),
            (b"keep-alive", b"timeout=5"),
            (b"transfer-encoding", b"chunked"),
            (b"content-type", b"application/json"),
            (b"host", b"localhost:8000"),
        ])
        await m.proxy_mcp_request(self._spec(), scope, _make_receive(b"{}"), sender)
        forwarded_lower = [k.lower() for k, _ in recorder.captured_headers]
        for banned in ("connection", "keep-alive", "transfer-encoding", "host"):
            assert banned not in forwarded_lower

    @pytest.mark.asyncio
    async def test_strips_hop_by_hop_response_headers(self):
        m = ProxyManager(mandatory_config_path="")
        recorder = _StreamRecorder(
            status_code=200,
            headers={
                "connection": "close",
                "transfer-encoding": "chunked",
                "x-custom": "ok",
                "content-type": "application/json",
            },
            chunks=[b"{}"],
        )
        m._http_client = recorder  # type: ignore[assignment]

        sender = _RecorderSend()
        await m.proxy_mcp_request(
            self._spec(),
            self._scope(headers=[]),
            _make_receive(b"{}"),
            sender,
        )
        # Hop-by-hop response headers must be stripped before relaying.
        assert sender.header_value("connection") is None
        assert sender.header_value("transfer-encoding") is None
        # Non-hop-by-hop response headers pass through untouched.
        assert sender.header_value("x-custom") == "ok"
        assert sender.header_value("content-type") == "application/json"

    @pytest.mark.asyncio
    async def test_default_accept_when_caller_omits(self):
        # MCP servers commonly require dual-Accept negotiation. If the
        # caller didn't set one, we inject the canonical pair so probe
        # tools / barebones HTTP clients still work.
        m = ProxyManager(mandatory_config_path="")
        recorder = _StreamRecorder(status_code=200, headers={}, chunks=[b"{}"])
        m._http_client = recorder  # type: ignore[assignment]

        sender = _RecorderSend()
        await m.proxy_mcp_request(
            self._spec(),
            self._scope(headers=[]),
            _make_receive(b"{}"),
            sender,
        )
        accepts = [v for k, v in recorder.captured_headers if k.lower() == "accept"]
        assert accepts == ["application/json, text/event-stream"]

    @pytest.mark.asyncio
    async def test_inbound_accept_preserved(self):
        m = ProxyManager(mandatory_config_path="")
        recorder = _StreamRecorder(status_code=200, headers={}, chunks=[b"{}"])
        m._http_client = recorder  # type: ignore[assignment]

        sender = _RecorderSend()
        await m.proxy_mcp_request(
            self._spec(),
            self._scope(headers=[(b"accept", b"text/event-stream")]),
            _make_receive(b"{}"),
            sender,
        )
        accepts = [v for k, v in recorder.captured_headers if k.lower() == "accept"]
        # Don't double-up — caller's Accept wins.
        assert accepts == ["text/event-stream"]

    @pytest.mark.asyncio
    async def test_503_when_http_client_not_initialised(self):
        m = ProxyManager(mandatory_config_path="")
        # Deliberately leave m._http_client = None.

        sender = _RecorderSend()
        await m.proxy_mcp_request(
            self._spec(),
            self._scope(headers=[]),
            _make_receive(b"{}"),
            sender,
        )
        assert sender.status == 503

    @pytest.mark.asyncio
    async def test_500_when_spec_has_no_url(self):
        m = ProxyManager(mandatory_config_path="")
        m._http_client = _StreamRecorder()  # type: ignore[assignment]

        # Build a malformed spec by clearing the URL after the fact.
        spec = self._spec()
        spec.url = None

        sender = _RecorderSend()
        await m.proxy_mcp_request(
            spec,
            self._scope(headers=[]),
            _make_receive(b"{}"),
            sender,
        )
        assert sender.status == 500

    @pytest.mark.asyncio
    async def test_per_request_authorization_wins_over_config_headers(self):
        # Config-level ``headers`` is for static augmentation (e.g.
        # custom X-Trace-Id). Per-request headers (especially
        # Authorization) MUST take priority so we never overwrite a
        # caller's bearer with a stale config-level one.
        m = ProxyManager(mandatory_config_path="")
        recorder = _StreamRecorder(status_code=200, headers={}, chunks=[b"{}"])
        m._http_client = recorder  # type: ignore[assignment]

        sender = _RecorderSend()
        await m.proxy_mcp_request(
            self._spec(headers={"X-Trace-Id": "abc", "Authorization": "should-be-ignored"}),
            self._scope(headers=[
                (b"authorization", b"Bearer caller-wins"),
            ]),
            _make_receive(b"{}"),
            sender,
        )
        # X-Trace-Id from config flows through.
        x_trace = [v for k, v in recorder.captured_headers if k.lower() == "x-trace-id"]
        assert x_trace == ["abc"]
        # Authorization from caller wins.
        sent_auth = [v for k, v in recorder.captured_headers if k.lower() == "authorization"]
        assert sent_auth == ["Bearer caller-wins"]


class TestMandatoryMerge:
    """Cover the mandatory-config merge applied at the top of start_all()."""

    @staticmethod
    def _write_mandatory(tmp_path, payload: dict | str) -> str:
        path = tmp_path / "mandatory.json"
        if isinstance(payload, str):
            path.write_text(payload)
        else:
            import json
            path.write_text(json.dumps(payload))
        return str(path)

    @pytest.mark.asyncio
    async def test_injects_missing_entries(self, tmp_path):
        mandatory_path = self._write_mandatory(tmp_path, {
            "mcpServers": {"required": {"command": "echo", "args": ["mandatory"]}}
        })
        with _patches()[0], _patches()[1], _patches()[2], _patches()[3], _patches()[4]:
            m = ProxyManager(mandatory_config_path=mandatory_path)
            await m.start_all({"mcpServers": {"alpha": {"command": "echo"}}})
            assert "required" in m._specs
            assert m._specs["required"].command == "echo"
            assert m._specs["required"].args == ["mandatory"]
            assert "alpha" in m._specs
            await m.stop_all()

    @pytest.mark.asyncio
    async def test_user_wins_on_name_collision(self, tmp_path):
        mandatory_path = self._write_mandatory(tmp_path, {
            "mcpServers": {
                "pincher": {"command": "pincher", "args": ["--from-mandatory"]}
            }
        })
        with _patches()[0], _patches()[1], _patches()[2], _patches()[3], _patches()[4]:
            m = ProxyManager(mandatory_config_path=mandatory_path)
            await m.start_all({
                "mcpServers": {
                    "pincher": {"command": "pincher", "args": ["--from-user"]}
                }
            })
            # User entry wins — args reflect the user payload, not the mandatory one.
            assert m._specs["pincher"].args == ["--from-user"]
            await m.stop_all()

    @pytest.mark.asyncio
    async def test_missing_file_passes_through(self, tmp_path):
        # Path that doesn't exist — merge is a no-op, no error raised.
        m = ProxyManager(mandatory_config_path=str(tmp_path / "does-not-exist.json"))
        merged = m._merge_mandatory({"mcpServers": {"alpha": {"command": "echo"}}})
        assert merged == {"mcpServers": {"alpha": {"command": "echo"}}}

    @pytest.mark.asyncio
    async def test_malformed_file_passes_through(self, tmp_path):
        bad = self._write_mandatory(tmp_path, "{this is not json")
        m = ProxyManager(mandatory_config_path=bad)
        merged = m._merge_mandatory({"mcpServers": {"alpha": {"command": "echo"}}})
        assert merged == {"mcpServers": {"alpha": {"command": "echo"}}}

    @pytest.mark.asyncio
    async def test_missing_mcpServers_in_mandatory_passes_through(self, tmp_path):
        # Mandatory file is valid JSON but lacks `mcpServers`.
        no_servers = self._write_mandatory(tmp_path, {"primaryMCP": "alpha"})
        m = ProxyManager(mandatory_config_path=no_servers)
        merged = m._merge_mandatory({"mcpServers": {"alpha": {"command": "echo"}}})
        assert merged == {"mcpServers": {"alpha": {"command": "echo"}}}

    def test_empty_path_disables_merge(self, tmp_path):
        # `mandatory_config_path=""` is the test-mode opt-out; even when a
        # default path would normally be discovered, this returns the input
        # unchanged. (We can't directly exercise auto-discover here without
        # mounting a real file, so this just confirms "" wins.)
        m = ProxyManager(mandatory_config_path="")
        raw = {"mcpServers": {"alpha": {"command": "echo"}}}
        assert m._merge_mandatory(raw) is raw

    @pytest.mark.asyncio
    async def test_user_wins_with_empty_mcpServers(self, tmp_path):
        """User payload missing the `mcpServers` key at all still gets the
        mandatory entries merged in (parse_config later requires non-empty,
        so this proves the merge happens before the validation gate)."""
        mandatory_path = self._write_mandatory(tmp_path, {
            "mcpServers": {"required": {"command": "echo"}}
        })
        m = ProxyManager(mandatory_config_path=mandatory_path)
        merged = m._merge_mandatory({})
        assert "mcpServers" in merged
        assert "required" in merged["mcpServers"]

    @pytest.mark.asyncio
    async def test_cache_is_reused_across_start_all_calls(self, tmp_path):
        mandatory_path = self._write_mandatory(tmp_path, {
            "mcpServers": {"required": {"command": "echo"}}
        })
        m = ProxyManager(mandatory_config_path=mandatory_path)
        first = m._read_mandatory_servers()
        # Delete the file — second call should still return the cached dict.
        import os
        os.unlink(mandatory_path)
        second = m._read_mandatory_servers()
        assert first is second  # cached object
        assert "required" in second

    def test_missing_fields_filled_from_mandatory_on_name_collision(self, tmp_path):
        """When the user includes a mandatory server name but omits fields
        present in the mandatory entry (e.g. reverseProxy), those fields are
        merged in from mandatory so infrastructure settings aren't silently
        dropped.  This is the root cause of the pincher-dashboard breakage when
        changing only the compression level."""
        mandatory_path = self._write_mandatory(tmp_path, {
            "mcpServers": {
                "pincher": {
                    "command": "pincher",
                    "args": ["--data-dir", "/tmp/pincher", "--http", "127.0.0.1:8080"],
                    "reverseProxy": {
                        "mount": "/pincher",
                        "upstream": "http://127.0.0.1:8080",
                    },
                    "compress": {"level": "medium"},
                }
            }
        })
        m = ProxyManager(mandatory_config_path=mandatory_path)
        # User only overrides the compression level — no reverseProxy supplied.
        merged = m._merge_mandatory({
            "mcpServers": {
                "pincher": {
                    "command": "pincher",
                    "args": ["--data-dir", "/tmp/pincher", "--http", "127.0.0.1:8080"],
                    "compress": {"level": "high"},
                }
            }
        })
        pincher = merged["mcpServers"]["pincher"]
        # User's compress value wins.
        assert pincher["compress"] == {"level": "high"}
        # Mandatory's reverseProxy is preserved even though user didn't include it.
        assert pincher["reverseProxy"] == {
            "mount": "/pincher",
            "upstream": "http://127.0.0.1:8080",
        }

    def test_user_explicit_field_wins_over_mandatory_on_collision(self, tmp_path):
        """Explicit user fields always win over mandatory fields of the same
        name — the field-level merge doesn't silently revert user changes."""
        mandatory_path = self._write_mandatory(tmp_path, {
            "mcpServers": {
                "pincher": {
                    "command": "pincher",
                    "reverseProxy": {"mount": "/pincher", "upstream": "http://127.0.0.1:8080"},
                    "compress": {"level": "medium"},
                }
            }
        })
        m = ProxyManager(mandatory_config_path=mandatory_path)
        merged = m._merge_mandatory({
            "mcpServers": {
                "pincher": {
                    "command": "pincher",
                    "reverseProxy": {"mount": "/custom", "upstream": "http://127.0.0.1:9090"},
                    "compress": {"level": "max"},
                }
            }
        })
        pincher = merged["mcpServers"]["pincher"]
        # User's reverseProxy wins on the entire key (top-level field merge).
        assert pincher["reverseProxy"]["mount"] == "/custom"
        assert pincher["compress"]["level"] == "max"
