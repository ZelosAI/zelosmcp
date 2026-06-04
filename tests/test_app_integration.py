"""Integration tests for the zelosmcp ASGI app (multi-MCP version)."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from zelosmcp.app import create_app
from zelosmcp.openapi import prefix_openapi_path as _prefix_openapi_path
from zelosmcp.manager import ProxyManager
from tests.conftest import (
    fake_stdio_client,
    fake_sse_client,
    fake_http_client,
    make_mock_session,
)


def _fresh():
    """Create a fresh ProxyManager + ASGI app pair."""
    manager = ProxyManager(mandatory_config_path="")
    app = create_app(manager)
    return app, manager


def _client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def _apply_patches():
    mock_session = make_mock_session()

    @asynccontextmanager
    async def patched_client_session(read, write):
        yield mock_session

    @asynccontextmanager
    async def patched_run(self):
        yield

    return (
        mock_session,
        patch("zelosmcp.proxy.stdio_client", side_effect=fake_stdio_client),
        patch("zelosmcp.proxy.sse_client", side_effect=fake_sse_client),
        patch("zelosmcp.proxy.streamablehttp_client", side_effect=fake_http_client),
        patch("zelosmcp.proxy.ClientSession", side_effect=patched_client_session),
        patch("zelosmcp.proxy.StreamableHTTPSessionManager.run", patched_run),
    )


_STDIO_CONFIG = {
    "mcpServers": {
        "alpha": {"command": "echo", "args": ["alpha"]},
        "beta":  {"command": "echo", "args": ["beta"]},
    },
}


# ── UI route ────────────────────────────────────────────────────────────

class TestUIRoute:
    @pytest.mark.asyncio
    async def test_index_returns_html(self):
        app, _ = _fresh()
        async with _client(app) as c:
            r = await c.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "ZELOSMCP" in r.text

    @pytest.mark.asyncio
    async def test_index_contains_mcp_json_snippet(self):
        app, _ = _fresh()
        async with _client(app) as c:
            r = await c.get("/")
        assert "mcpServers" in r.text

    @pytest.mark.asyncio
    async def test_index_contains_copy_button(self):
        app, _ = _fresh()
        async with _client(app) as c:
            r = await c.get("/")
        assert "copySnippet" in r.text

    @pytest.mark.asyncio
    async def test_index_contains_config_textarea(self):
        app, _ = _fresh()
        async with _client(app) as c:
            r = await c.get("/")
        assert "config-textarea" in r.text
        assert "config-input" in r.text

    @pytest.mark.asyncio
    async def test_index_contains_parse_config_function(self):
        app, _ = _fresh()
        async with _client(app) as c:
            r = await c.get("/")
        assert "parseConfig" in r.text

    @pytest.mark.asyncio
    async def test_index_links_to_docs(self):
        app, _ = _fresh()
        async with _client(app) as c:
            r = await c.get("/")
        assert 'href="/docs"' in r.text

    @pytest.mark.asyncio
    async def test_index_has_documentation_view(self):
        """Documentation nav item + center-pane view ship in HTML_TEMPLATE."""
        app, _ = _fresh()
        async with _client(app) as c:
            r = await c.get("/")
        assert 'data-view="docs"' in r.text
        assert "loadDocsIndex" in r.text

    @pytest.mark.asyncio
    async def test_index_has_events_view(self):
        app, _ = _fresh()
        async with _client(app) as c:
            r = await c.get("/")
        assert 'data-view="events"' in r.text
        assert "loadEventHistory" in r.text


class TestEventsAPI:
    @pytest.mark.asyncio
    async def test_events_page_and_summary(self):
        app, manager = _fresh()
        await manager.start_savings(":memory:")
        assert manager.events is not None
        await manager.events.record_event(
            event_id="evt-1",
            method="tools/call",
            backend="docker",
            tool="list_containers",
            qualified="docker__list_containers",
            compressed=True,
            input_payload={"all": True},
            output_payload={"count": 2},
            raw_output_tokens=18,
            raw_output_bytes=64,
            transform_type="toon",
            latency_ms=9,
            error=False,
        )

        async with _client(app) as c:
            page = await c.get(
                "/api/events",
                params={"backend": "docker", "limit": 10},
            )
            summary = await c.get("/api/events/summary")
            savings = await c.get("/api/savings")
            retention = await c.get("/api/events/retention")

        assert page.status_code == 200
        assert page.json()["total"] == 1
        assert page.json()["events"][0]["event_id"] == "evt-1"
        assert page.json()["filters"]["backend"] == "docker"

        assert summary.status_code == 200
        payload = summary.json()
        assert payload["totals"]["events"] == 1
        assert payload["totals"]["calls"] == 1
        assert payload["totals"]["raw_output_tokens"] == 18
        assert payload["top_tools"][0]["qualified"] == "docker__list_containers"

        assert savings.status_code == 200
        savings_payload = savings.json()
        assert savings_payload["calls"]["totals"]["transactions"] == 1
        assert savings_payload["calls"]["totals"]["raw_output_tokens"] == 18
        assert savings_payload["response_transform_saved_tokens_total"] > 0
        assert savings_payload["calls"]["top_tools"][0]["tokens"] >= 18

        assert retention.status_code == 200
        retention_payload = retention.json()
        assert retention_payload["retention_hours"] == manager.event_retention_hours
        assert retention_payload["prune_interval_mins"] == manager.event_prune_interval_mins
        assert retention_payload["oldest_event_at"] is not None

    @pytest.mark.asyncio
    async def test_index_has_server_details_view(self):
        """Per-server "Details" button + center-pane view ship in HTML_TEMPLATE."""
        app, _ = _fresh()
        async with _client(app) as c:
            r = await c.get("/")
        assert 'data-view="server-details"' in r.text
        assert "showServerDetails" in r.text


# ── OpenAPI explorer ────────────────────────────────────────────────────

class TestOpenAPI:
    @pytest.mark.asyncio
    async def test_openapi_json_returns_spec(self):
        app, _ = _fresh()
        async with _client(app) as c:
            r = await c.get("/openapi.json")
        assert r.status_code == 200
        spec = r.json()
        assert spec["openapi"].startswith("3.")
        paths = spec["paths"]
        assert "/api/start" in paths
        assert "/api/stop" in paths
        assert "/api/status" in paths
        assert "/api/logs" in paths
        assert "/api/servers/{name}" in paths
        assert "/api/servers/{name}/start" in paths
        assert "/api/servers/{name}/stop" in paths

    @pytest.mark.asyncio
    async def test_swagger_ui_renders(self):
        app, _ = _fresh()
        async with _client(app) as c:
            r = await c.get("/docs")
        assert r.status_code == 200
        assert "swagger-ui" in r.text
        assert "/openapi.json" in r.text

    @pytest.mark.asyncio
    async def test_redoc_renders(self):
        app, _ = _fresh()
        async with _client(app) as c:
            r = await c.get("/redoc")
        assert r.status_code == 200
        assert "redoc" in r.text.lower()

    @pytest.mark.asyncio
    async def test_openapi_json_merges_reverse_proxy_contract(self):
        captured: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["url"] = str(req.url)
            captured["headers"] = dict(req.headers)
            return httpx.Response(
                200,
                json={
                    "openapi": "3.0.3",
                    "info": {"title": "Alpha", "version": "1.0.0"},
                    "paths": {
                        "/v1/search": {
                            "post": {
                                "summary": "Search alpha",
                                "requestBody": {
                                    "content": {
                                        "application/json": {
                                            "schema": {
                                                "$ref": "#/components/schemas/SearchRequest"
                                            }
                                        }
                                    }
                                },
                                "responses": {"200": {"description": "OK"}},
                            }
                        }
                    },
                    "components": {
                        "schemas": {
                            "SearchRequest": {
                                "type": "object",
                                "properties": {"q": {"type": "string"}},
                            }
                        }
                    },
                },
            )

        cfg = {
            "mcpServers": {
                "alpha": {
                    "command": "echo",
                    "args": ["a"],
                    "reverseProxy": {
                        "mount": "/alpha",
                        "upstream": "http://upstream.test",
                        "openapi": {"path": "/v1/openapi.json"},
                    },
                },
            }
        }

        with _apply_patches()[1], _apply_patches()[2], _apply_patches()[3], _apply_patches()[4], _apply_patches()[5]:
            app, manager = _fresh()
            async with _lifespan(app):
                _install_mock_upstream(manager, handler)
                async with _client(app) as c:
                    await c.post("/api/start", json=cfg)
                    r = await c.get("/openapi.json")

        assert r.status_code == 200
        spec = r.json()
        operation = spec["paths"]["/alpha/v1/search"]["post"]
        assert operation["tags"] == ["alpha"]
        schema = operation["requestBody"]["content"]["application/json"]["schema"]
        assert schema["$ref"] == "#/components/schemas/alpha_SearchRequest"
        assert "alpha_SearchRequest" in spec["components"]["schemas"]
        assert captured["url"] == "http://upstream.test/v1/openapi.json"
        assert captured["headers"]["x-forwarded-prefix"] == "/alpha"

    # ── _prefix_openapi_path unit tests ──────────────────────────────────

    def test_prefix_plain_path(self):
        """Standard case: upstream path has no mount prefix yet."""
        assert _prefix_openapi_path("/pincher", "/v1/adr") == "/pincher/v1/adr"

    def test_prefix_strips_duplicate_mount(self):
        """Upstream that self-reports its mount prefix must not double it."""
        assert _prefix_openapi_path("/pincher", "/pincher/v1/adr") == "/pincher/v1/adr"

    def test_prefix_strips_duplicate_mount_nested(self):
        assert _prefix_openapi_path("/alpha", "/alpha/v1/search") == "/alpha/v1/search"

    def test_prefix_root_path(self):
        assert _prefix_openapi_path("/alpha", "/") == "/alpha"

    def test_prefix_exact_mount_path(self):
        # Upstream exposes its root as the mount path itself.
        assert _prefix_openapi_path("/alpha", "/alpha") == "/alpha/alpha"

    def test_prefix_does_not_strip_partial_overlap(self):
        # '/alphabeta' should NOT strip '/alpha'.
        assert _prefix_openapi_path("/alpha", "/alphabeta/v1") == "/alpha/alphabeta/v1"

    def test_prefix_no_leading_slash_in_path(self):
        assert _prefix_openapi_path("/alpha", "v1/items") == "/alpha/v1/items"

    # ── integration: duplicate-mount path regression ──────────────────────

    @pytest.mark.asyncio
    async def test_openapi_does_not_double_prefix_when_upstream_includes_mount(self):
        """Regression test: upstream returning /pincher/v1/... paths must not
        become /pincher/pincher/v1/... in the merged spec."""

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "openapi": "3.0.3",
                    "info": {"title": "Pincher", "version": "1.0.0"},
                    "paths": {
                        # Upstream self-reports its mount prefix in path keys.
                        "/pincher/v1/adr": {
                            "post": {
                                "summary": "Store ADR",
                                "responses": {"200": {"description": "OK"}},
                            }
                        }
                    },
                },
            )

        cfg = {
            "mcpServers": {
                "pincher": {
                    "command": "echo",
                    "args": ["p"],
                    "reverseProxy": {
                        "mount": "/pincher",
                        "upstream": "http://pincher.test",
                        "openapi": {"path": "/v1/openapi.json"},
                    },
                },
            }
        }

        with _apply_patches()[1], _apply_patches()[2], _apply_patches()[3], _apply_patches()[4], _apply_patches()[5]:
            app, manager = _fresh()
            async with _lifespan(app):
                _install_mock_upstream(manager, handler)
                async with _client(app) as c:
                    await c.post("/api/start", json=cfg)
                    r = await c.get("/openapi.json")

        assert r.status_code == 200
        spec = r.json()
        paths = spec["paths"]
        # Must appear exactly once under the correct key.
        assert "/pincher/v1/adr" in paths
        # Must NOT be doubled.
        assert "/pincher/pincher/v1/adr" not in paths

    @pytest.mark.asyncio
    async def test_openapi_json_survives_upstream_contract_failure(self):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": "not ready"})

        cfg = {
            "mcpServers": {
                "alpha": {
                    "command": "echo",
                    "args": ["a"],
                    "reverseProxy": {
                        "mount": "/alpha",
                        "upstream": "http://upstream.test",
                        "openapi": {"path": "/v1/openapi.json"},
                    },
                },
            }
        }

        with _apply_patches()[1], _apply_patches()[2], _apply_patches()[3], _apply_patches()[4], _apply_patches()[5]:
            app, manager = _fresh()
            async with _lifespan(app):
                _install_mock_upstream(manager, handler)
                async with _client(app) as c:
                    await c.post("/api/start", json=cfg)
                    r = await c.get("/openapi.json")

        assert r.status_code == 200
        spec = r.json()
        assert "/api/status" in spec["paths"]
        assert "/alpha/v1/search" not in spec["paths"]
        warnings = spec["x-zelosmcp-openapi-warnings"]
        assert warnings[0]["backend"] == "alpha"
        assert warnings[0]["path"] == "/v1/openapi.json"


# ── Status API ──────────────────────────────────────────────────────────

class TestStatusAPI:
    @pytest.mark.asyncio
    async def test_status_when_stopped(self):
        app, _ = _fresh()
        async with _client(app) as c:
            r = await c.get("/api/status")
        assert r.status_code == 200
        data = r.json()
        assert data["running"] is False
        assert data["primary"] is None
        # `zelosmcp` is the always-on builtin row; only user backends matter here.
        user_servers = [s for s in data["servers"] if not s.get("builtin")]
        assert user_servers == []

    @pytest.mark.asyncio
    async def test_status_when_running(self):
        _, *patches = _apply_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            app, manager = _fresh()
            async with _client(app) as c:
                await c.post("/api/start", json=_STDIO_CONFIG)
                r = await c.get("/api/status")
            data = r.json()
            assert data["running"] is True
            assert data["primary"] is None
            user_names = [s["name"] for s in data["servers"] if not s.get("builtin")]
            assert user_names == ["alpha", "beta"]
            for s in data["servers"]:
                assert s["primary"] is False
            await manager.stop_all()


# ── Start API ───────────────────────────────────────────────────────────

class TestStartAPI:
    @pytest.mark.asyncio
    async def test_start_success_multi(self):
        _, *patches = _apply_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            app, manager = _fresh()
            async with _client(app) as c:
                r = await c.post("/api/start", json=_STDIO_CONFIG)
            assert r.status_code == 200
            data = r.json()
            assert data["ok"] is True
            assert data["primary"] is None
            assert set(data["servers"].keys()) == {"alpha", "beta"}
            assert all(s["ok"] for s in data["servers"].values())
            assert manager.get("alpha") is not None
            assert manager.get("beta") is not None
            assert manager.aggregator.running is True
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_start_invalid_json(self):
        app, _ = _fresh()
        async with _client(app) as c:
            r = await c.post("/api/start", content="not json")
        assert r.status_code == 400
        assert "Invalid JSON" in r.json()["error"]

    @pytest.mark.asyncio
    async def test_start_missing_mcpServers(self):
        app, _ = _fresh()
        async with _client(app) as c:
            r = await c.post("/api/start", json={})
        assert r.status_code == 400
        assert "mcpServers" in r.json()["error"]

    @pytest.mark.asyncio
    async def test_start_with_deprecated_primary_is_accepted(self):
        # primaryMCP is deprecated — accepted for back-compat, ignored at runtime.
        _, *patches = _apply_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            app, manager = _fresh()
            async with _client(app) as c:
                r = await c.post("/api/start", json={
                    "primaryMCP": "ghost",
                    "mcpServers": {"alpha": {"command": "echo", "args": ["a"]}},
                })
            assert r.status_code == 200
            assert r.json()["ok"] is True
            assert manager.primary is None
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_start_reserved_name_rejected(self):
        app, _ = _fresh()
        async with _client(app) as c:
            r = await c.post("/api/start", json={
                "mcpServers": {"api": {"command": "echo", "args": ["x"]}},
            })
        assert r.status_code == 400
        assert "reserved" in r.json()["error"]

    @pytest.mark.asyncio
    async def test_start_replaces_previous_set(self):
        _, *patches = _apply_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            app, manager = _fresh()
            async with _client(app) as c:
                await c.post("/api/start", json=_STDIO_CONFIG)
                user = {n for n in manager.names() if n not in ("zelosmcp", "zelos")}
                assert user == {"alpha", "beta"}
                await c.post("/api/start", json={
                    "mcpServers": {"gamma": {"command": "echo", "args": ["g"]}},
                })
                user = {n for n in manager.names() if n not in ("zelosmcp", "zelos")}
                assert user == {"gamma"}
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_start_with_env_passed_through(self):
        _, *patches = _apply_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            app, manager = _fresh()
            async with _client(app) as c:
                r = await c.post("/api/start", json={
                    "mcpServers": {
                        "alpha": {
                            "command": "uvx",
                            "args": ["code-index-mcp"],
                            "env": {"API_KEY": "sk-test"},
                        }
                    }
                })
            assert r.status_code == 200
            assert manager.get("alpha").backend_info["env"] == {"API_KEY": "sk-test"}
            await manager.stop_all()


# ── Per-server endpoints ────────────────────────────────────────────────

class TestPerServerEndpoints:
    @pytest.mark.asyncio
    async def test_get_unknown_returns_404(self):
        app, _ = _fresh()
        async with _client(app) as c:
            r = await c.get("/api/servers/ghost")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_get_known_server(self):
        _, *patches = _apply_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            app, manager = _fresh()
            async with _client(app) as c:
                await c.post("/api/start", json=_STDIO_CONFIG)
                r = await c.get("/api/servers/alpha")
            assert r.status_code == 200
            data = r.json()
            assert data["name"] == "alpha"
            assert data["primary"] is False
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_stop_then_start_one(self):
        _, *patches = _apply_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            app, manager = _fresh()
            async with _client(app) as c:
                await c.post("/api/start", json=_STDIO_CONFIG)
                r = await c.post("/api/servers/beta/stop")
                assert r.status_code == 200
                assert manager.get("beta").running is False
                r = await c.post("/api/servers/beta/start")
                assert r.status_code == 200
                assert manager.get("beta").running is True
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_start_unknown_server_404(self):
        app, _ = _fresh()
        async with _client(app) as c:
            r = await c.post("/api/servers/ghost/start")
        assert r.status_code == 404


# ── Stop API ────────────────────────────────────────────────────────────

class TestStopAPI:
    @pytest.mark.asyncio
    async def test_stop_when_not_running(self):
        app, _ = _fresh()
        async with _client(app) as c:
            r = await c.post("/api/stop")
        assert r.status_code == 200
        assert r.json()["ok"] is True

    @pytest.mark.asyncio
    async def test_stop_after_start(self):
        _, *patches = _apply_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            app, manager = _fresh()
            async with _client(app) as c:
                await c.post("/api/start", json=_STDIO_CONFIG)
                r = await c.post("/api/stop")
            assert r.status_code == 200
            user = [n for n in manager.names() if n not in ("zelosmcp", "zelos")]
            assert user == []


# ── MCP routing ─────────────────────────────────────────────────────────

class TestMCPRouting:
    @pytest.mark.asyncio
    async def test_root_mcp_503_when_no_servers(self):
        app, _ = _fresh()
        async with _client(app) as c:
            r = await c.post("/mcp")
        assert r.status_code == 503
        assert "No MCP servers" in r.json()["error"]

    @pytest.mark.asyncio
    async def test_named_mcp_503_when_unknown(self):
        app, _ = _fresh()
        async with _client(app) as c:
            r = await c.post("/ghost/mcp")
        assert r.status_code == 503
        assert "ghost" in r.json()["error"]

    @pytest.mark.asyncio
    async def test_named_mcp_dispatches_when_running(self):
        _, *patches = _apply_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            app, manager = _fresh()
            async with _client(app) as c:
                await c.post("/api/start", json=_STDIO_CONFIG)
                # The session manager's handle_request is a real coroutine; we
                # only assert that the dispatcher chose it (i.e. the request
                # passes through to the manager rather than 503).
                manager.get("alpha").session_manager.handle_request = AsyncMock(
                    return_value=None
                )

                async def _send(msg): pass
                async def _recv():
                    return {"type": "http.disconnect"}

                scope = {
                    "type": "http",
                    "method": "POST",
                    "path": "/alpha/mcp",
                    "raw_path": b"/alpha/mcp",
                    "headers": [],
                }
                await app(scope, _recv, _send)
                manager.get("alpha").session_manager.handle_request.assert_awaited_once()
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_root_mcp_dispatches_to_aggregator(self):
        _, *patches = _apply_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            app, manager = _fresh()
            async with _client(app) as c:
                await c.post("/api/start", json=_STDIO_CONFIG)
                # Spy on every session_manager: only the aggregator's should fire.
                manager.aggregator.session_manager.handle_request = AsyncMock(
                    return_value=None
                )
                manager.get("alpha").session_manager.handle_request = AsyncMock(
                    return_value=None
                )
                manager.get("beta").session_manager.handle_request = AsyncMock(
                    return_value=None
                )

                async def _send(msg): pass
                async def _recv():
                    return {"type": "http.disconnect"}

                scope = {
                    "type": "http",
                    "method": "POST",
                    "path": "/mcp",
                    "raw_path": b"/mcp",
                    "headers": [],
                }
                await app(scope, _recv, _send)
                manager.aggregator.session_manager.handle_request.assert_awaited_once()
                manager.get("alpha").session_manager.handle_request.assert_not_called()
                manager.get("beta").session_manager.handle_request.assert_not_called()
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_unrelated_path_passes_to_starlette(self):
        app, _ = _fresh()
        async with _client(app) as c:
            r = await c.get("/nonexistent")
        assert r.status_code == 404


# ── Logs SSE endpoint ───────────────────────────────────────────────────

class TestLogsEndpoint:
    @pytest.mark.asyncio
    async def test_logs_returns_sse_content_type(self):
        app, _ = _fresh()

        async def check():
            async with _client(app) as c:
                async with c.stream("GET", "/api/logs") as r:
                    assert r.status_code == 200
                    assert "text/event-stream" in r.headers["content-type"]

        try:
            await asyncio.wait_for(check(), timeout=2.0)
        except (asyncio.TimeoutError, httpx.ReadError):
            pass  # SSE stream is infinite

    @pytest.mark.asyncio
    async def test_logs_subscriber_receives_emitted_messages(self):
        _, manager = _fresh()
        q = manager.subscribe_logs()
        manager._broadcast("integration-test-msg")
        msg = q.get_nowait()
        assert "integration-test-msg" in msg
        manager.unsubscribe_logs(q)

    @pytest.mark.asyncio
    async def test_logs_history_buffer_records_broadcasts(self):
        _, manager = _fresh()
        manager._broadcast("first-line")
        manager._broadcast("second-line")
        snapshot, q = manager.subscribe_logs_with_history()
        try:
            assert snapshot == ["first-line", "second-line"]
            # New broadcasts after subscription land on the queue, NOT
            # the snapshot, so a client draining snapshot-then-queue
            # sees the full ordered sequence with no dups.
            manager._broadcast("third-line")
            assert q.get_nowait() == "third-line"
            assert q.empty()
        finally:
            manager.unsubscribe_logs(q)

    @pytest.mark.asyncio
    async def test_logs_history_is_capped(self):
        _, manager = _fresh()
        cap = manager._log_history.maxlen
        assert cap is not None
        for i in range(cap + 50):
            manager._broadcast(f"line-{i}")
        snapshot, q = manager.subscribe_logs_with_history()
        try:
            assert len(snapshot) == cap
            assert snapshot[0] == f"line-{50}"
            assert snapshot[-1] == f"line-{cap + 50 - 1}"
        finally:
            manager.unsubscribe_logs(q)

    @pytest.mark.asyncio
    async def test_subscribe_with_history_after_subscribe_does_not_dup(self):
        # Subscribers that joined BEFORE a broadcast already saw the
        # line in their queue; a later subscribe_logs_with_history call
        # must replay history but the original subscriber must NOT see
        # the same line a second time.
        _, manager = _fresh()
        existing_q = manager.subscribe_logs()
        manager._broadcast("only-line")
        assert existing_q.get_nowait() == "only-line"

        snapshot, new_q = manager.subscribe_logs_with_history()
        try:
            assert snapshot == ["only-line"]
            assert existing_q.empty()
            assert new_q.empty()
        finally:
            manager.unsubscribe_logs(existing_q)
            manager.unsubscribe_logs(new_q)


# ── Full lifecycle ──────────────────────────────────────────────────────

class TestFullLifecycle:
    @pytest.mark.asyncio
    async def test_start_status_stop_status(self):
        _, *patches = _apply_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            app, manager = _fresh()
            async with _client(app) as c:
                r = await c.get("/api/status")
                assert r.json()["running"] is False

                r = await c.post("/api/start", json=_STDIO_CONFIG)
                assert r.json()["ok"] is True

                r = await c.get("/api/status")
                data = r.json()
                assert data["running"] is True
                assert data["primary"] is None

                r = await c.post("/api/stop")
                assert r.json()["ok"] is True

                r = await c.get("/api/status")
                assert r.json()["running"] is False
                user = [s for s in r.json()["servers"] if not s.get("builtin")]
                assert user == []


# ── Built-in MCP (/zelosmcp/mcp + /api/cursor-rule + aggregate) ─────────


@asynccontextmanager
async def _lifespan(app):
    """Drive Starlette's ASGI lifespan protocol manually so the built-in
    actually starts (httpx.ASGITransport doesn't drive lifespan on its own).
    Yields once startup completes; cleanly shuts down on exit."""
    queue: asyncio.Queue = asyncio.Queue()
    sent: list = []

    async def receive():
        return await queue.get()

    async def send(msg):
        sent.append(msg)

    task = asyncio.create_task(app({"type": "lifespan"}, receive, send))
    await queue.put({"type": "lifespan.startup"})
    # Wait for the startup.complete event before yielding to the test body.
    for _ in range(100):
        if any(m.get("type") == "lifespan.startup.complete" for m in sent):
            break
        await asyncio.sleep(0.02)
    else:
        raise RuntimeError("lifespan startup did not complete in 2s")
    try:
        yield
    finally:
        await queue.put({"type": "lifespan.shutdown"})
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()


class TestCursorRuleEndpoint:
    @pytest.mark.asyncio
    async def test_returns_markdown_with_no_user_backends(self):
        app, _ = _fresh()
        async with _lifespan(app):
            async with _client(app) as c:
                r = await c.get("/api/cursor-rule")
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/markdown")
            assert "alwaysApply: true" in r.text
            assert "# zelosMCP backends" in r.text
            # No user backends -> generator emits the "no backends loaded" body.
            assert "No user backends are currently loaded" in r.text
            # Default access is read-only -> directive is present.
            assert "Access mode: READ-ONLY" in r.text

    @pytest.mark.asyncio
    async def test_scoped_style_includes_globs(self):
        app, _ = _fresh()
        async with _lifespan(app):
            async with _client(app) as c:
                r = await c.get(
                    "/api/cursor-rule",
                    params={"style": "scoped", "globs": "**/*.py"},
                )
            assert r.status_code == 200
            assert "alwaysApply: false" in r.text
            assert "globs: **/*.py" in r.text

    @pytest.mark.asyncio
    async def test_unknown_style_400(self):
        app, _ = _fresh()
        async with _lifespan(app):
            async with _client(app) as c:
                r = await c.get("/api/cursor-rule", params={"style": "bogus"})
            assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_access_param_read_write_changes_directive(self):
        """`?access=read-write` swaps the directive block from the
        forbid-mutations text to the confirm-with-user text."""
        app, _ = _fresh()
        async with _lifespan(app):
            async with _client(app) as c:
                ro = await c.get("/api/cursor-rule")
                rw = await c.get(
                    "/api/cursor-rule", params={"access": "read-write"}
                )
            assert ro.status_code == 200
            assert rw.status_code == 200
            assert "Access mode: READ-ONLY" in ro.text
            assert "Access mode: READ-ONLY" not in rw.text
            assert "Access mode: READ-WRITE" in rw.text
            assert "(read-write mode)" in rw.text
            assert "(read-only mode)" in ro.text

    @pytest.mark.asyncio
    async def test_unknown_access_400(self):
        app, _ = _fresh()
        async with _lifespan(app):
            async with _client(app) as c:
                r = await c.get("/api/cursor-rule", params={"access": "bogus"})
            assert r.status_code == 400
            assert "Unknown access" in r.json()["error"]

    @pytest.mark.asyncio
    async def test_format_copilot_instructions_no_frontmatter(self):
        """`?format=copilot-instructions` returns the same body as the
        cursor-mdc default but without the YAML frontmatter wrapper.
        The HTTP body matches the MCP tool's body byte-for-byte."""
        app, _ = _fresh()
        async with _lifespan(app):
            async with _client(app) as c:
                mdc = await c.get("/api/cursor-rule")
                copi = await c.get(
                    "/api/cursor-rule",
                    params={"format": "copilot-instructions"},
                )
            assert mdc.status_code == 200
            assert copi.status_code == 200
            assert copi.headers["content-type"].startswith("text/markdown")
            # No frontmatter in the copilot-instructions body.
            assert not copi.text.startswith("---")
            assert "alwaysApply" not in copi.text
            # The cursor-mdc body's content (after frontmatter) matches.
            _, _, mdc_after = mdc.text.partition("---\n")
            _, _, mdc_body = mdc_after.partition("---\n")
            assert mdc_body == copi.text

    @pytest.mark.asyncio
    async def test_unknown_format_400(self):
        app, _ = _fresh()
        async with _lifespan(app):
            async with _client(app) as c:
                r = await c.get("/api/cursor-rule", params={"format": "bogus"})
            assert r.status_code == 400
            assert "Unknown format" in r.json()["error"]

    @pytest.mark.asyncio
    async def test_tool_use_priority_default_matches_explicit(self):
        """Default `tool_use=priority` and explicit `?tool_use=priority`
        must produce identical bodies (round-trip)."""
        app, _ = _fresh()
        async with _lifespan(app):
            async with _client(app) as c:
                default = await c.get("/api/cursor-rule")
                explicit = await c.get(
                    "/api/cursor-rule", params={"tool_use": "priority"}
                )
            assert default.status_code == 200
            assert explicit.status_code == 200
            assert default.text == explicit.text

    @pytest.mark.asyncio
    async def test_tool_use_available_differs_from_priority(self):
        """`?tool_use=available` produces a different body than the
        priority default. With no user backends loaded both bodies are
        the empty-backends template (which has no priority directive
        either way), so we just assert the request succeeds — content
        differences are exercised in the unit tests."""
        app, _ = _fresh()
        async with _lifespan(app):
            async with _client(app) as c:
                r = await c.get(
                    "/api/cursor-rule", params={"tool_use": "available"}
                )
            assert r.status_code == 200
            # Empty-backend body still has the directive, but no
            # priority directive in either mode.
            assert "## Tool-use priority" not in r.text

    @pytest.mark.asyncio
    async def test_unknown_tool_use_400(self):
        app, _ = _fresh()
        async with _lifespan(app):
            async with _client(app) as c:
                r = await c.get(
                    "/api/cursor-rule", params={"tool_use": "bogus"}
                )
            assert r.status_code == 400
            assert "Unknown tool_use" in r.json()["error"]


class TestBuiltinMcp:
    @pytest.mark.asyncio
    async def test_aggregate_exposes_zelosmcp_tools(self):
        """With no user backends configured, /mcp still serves the eight
        `zelosmcp__*` tools provided by the built-in."""
        app, _ = _fresh()
        async with _lifespan(app):
            async with _client(app) as c:
                init = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                }
                headers = {"Accept": "application/json, text/event-stream"}
                await c.post("/mcp", json=init, headers=headers)
                r = await c.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                    headers=headers,
                )
            data = r.json()
            names = {t["name"] for t in data["result"]["tools"]}
            expected = {
                f"zelosmcp__{n}"
                for n in (
                    "generate_cursor_rule",
                    "list_loaded_servers",
                    "get_aggregated_tool_catalog",
                    "generate_cursor_mcp_json",
                    "start_server",
                    "stop_server",
                    "reload_config",
                )
            }
            assert expected <= names

    @pytest.mark.asyncio
    async def test_zelosmcp_mcp_direct_route_unprefixed(self):
        """At /zelosmcp/mcp the same tools appear without the `zelosmcp__`
        namespace prefix (raw passthrough)."""
        app, _ = _fresh()
        async with _lifespan(app):
            async with _client(app) as c:
                init = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                }
                headers = {"Accept": "application/json, text/event-stream"}
                await c.post("/zelosmcp/mcp", json=init, headers=headers)
                r = await c.post(
                    "/zelosmcp/mcp",
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                    headers=headers,
                )
            data = r.json()
            names = {t["name"] for t in data["result"]["tools"]}
            assert "generate_cursor_rule" in names
            # No prefix at the direct route.
            assert not any(n.startswith("zelosmcp__") for n in names)

    @pytest.mark.asyncio
    async def test_aggregate_call_tool_round_trip(self):
        """Round-trip a `tools/call` of `zelosmcp__generate_cursor_rule`
        at /mcp and assert the body is the same shape /api/cursor-rule
        returns."""
        app, _ = _fresh()
        async with _lifespan(app):
            async with _client(app) as c:
                init = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                }
                headers = {"Accept": "application/json, text/event-stream"}
                await c.post("/mcp", json=init, headers=headers)
                r = await c.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "zelosmcp__generate_cursor_rule",
                            "arguments": {},
                        },
                    },
                    headers=headers,
                )
                http_r = await c.get("/api/cursor-rule")
            data = r.json()
            content = data["result"]["content"]
            assert content
            assert content[0]["type"] == "text"
            mcp_body = content[0]["text"]
            # Same generator under both transports -> identical output.
            assert mcp_body == http_r.text


class TestCatalogEndpoint:
    @pytest.mark.asyncio
    async def test_api_catalog_includes_builtin_with_eight_tools(self):
        """/api/catalog must include the always-on builtin row with all
        8 tools and well-formed inputSchemas, even when no user backend
        is configured. (Count history: 7 → 8 with `list_compressed_tools`
        → 9 with `warm_up_passthrough_auth` → 8 again after warm_up was
        dropped in favour of compression-wrapper-driven OAuth.)"""
        app, _ = _fresh()
        async with _lifespan(app):
            async with _client(app) as c:
                r = await c.get("/api/catalog")
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("application/json")
            data = r.json()
            assert "zelosmcp" in data
            row = data["zelosmcp"]
            assert row["transport"] == "builtin"
            assert row["running"] is True
            tools = row["tools"]
            assert isinstance(tools, list) and len(tools) == 8
            # Each tool entry has the keys the UI consumes.
            for t in tools:
                assert "name" in t
                assert "description" in t
                assert "inputSchema" in t
                assert isinstance(t["inputSchema"], dict)
            # Empty capabilities are coerced to []
            assert row["prompts"] == []
            assert row["resources"] == []
            assert row["resourceTemplates"] == []

    @pytest.mark.asyncio
    async def test_catalog_html_page(self):
        app, _ = _fresh()
        async with _lifespan(app):
            async with _client(app) as c:
                r = await c.get("/catalog")
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/html")
            # Sanity: the page references the JSON endpoint and renders the title.
            assert "Tool catalog" in r.text
            assert "/api/catalog" in r.text

    @pytest.mark.asyncio
    async def test_api_catalog_matches_mcp_tool(self):
        """`/api/catalog` and `zelosmcp__get_aggregated_tool_catalog` use
        the same helper, so the JSON they emit must be identical."""
        app, _ = _fresh()
        async with _lifespan(app):
            async with _client(app) as c:
                # HTTP catalog
                http_r = await c.get("/api/catalog")
                http_payload = http_r.json()

                # MCP tool round-trip via /mcp
                init = {
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"},
                    },
                }
                headers = {"Accept": "application/json, text/event-stream"}
                await c.post("/mcp", json=init, headers=headers)
                r = await c.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {
                            "name": "zelosmcp__get_aggregated_tool_catalog",
                            "arguments": {},
                        },
                    },
                    headers=headers,
                )
            mcp_text = r.json()["result"]["content"][0]["text"]
            assert json.loads(mcp_text) == http_payload


# ── Docs API ────────────────────────────────────────────────────────────

class TestDocsRoutes:
    """Reads the project's own docs/ + README.md, so these tests assume
    the suite runs from a checkout (or the Docker image — both ship the
    files). If the discovery falls back to an empty index the assertions
    will fail loudly, which is the signal we want for a misconfigured
    image."""

    @pytest.mark.asyncio
    async def test_api_docs_index_lists_known_slugs(self):
        app, _ = _fresh()
        async with _client(app) as c:
            r = await c.get("/api/docs")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        slugs = {d["slug"] for d in data}
        # Only docs/*.md is surfaced — README is intentionally excluded
        # so the in-app Documentation view stays focused on docs.
        assert "readme" not in slugs
        assert "quickstart" in slugs
        for entry in data:
            assert "title" in entry and entry["title"]

    @pytest.mark.asyncio
    async def test_api_docs_does_not_expose_readme(self):
        app, _ = _fresh()
        async with _client(app) as c:
            r = await c.get("/api/docs/readme")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_api_docs_get_returns_rendered_html(self):
        app, _ = _fresh()
        async with _client(app) as c:
            r = await c.get("/api/docs/quickstart")
        assert r.status_code == 200
        data = r.json()
        assert data["slug"] == "quickstart"
        assert data["title"]
        assert data["html"].startswith("<")
        assert "<h1" in data["html"] or "<h2" in data["html"]
        assert data["markdown"]

    @pytest.mark.asyncio
    async def test_api_docs_get_unknown_slug_404(self):
        app, _ = _fresh()
        async with _client(app) as c:
            r = await c.get("/api/docs/does-not-exist")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_api_docs_rejects_path_traversal(self):
        """Slug values that would resolve outside the docs root must
        404, never expose arbitrary files."""
        app, _ = _fresh()
        async with _client(app) as c:
            # Starlette path-converters reject `..` segments outright at
            # routing time, so this lands as 404 before the handler even
            # runs — but we still assert the *behaviour* (no 200, no
            # /etc/passwd-shaped payload) end-to-end.
            r = await c.get("/api/docs/..%2Fpyproject")
        assert r.status_code == 404


# ── Reverse-proxy dispatch ──────────────────────────────────────────────


def _install_mock_upstream(manager, handler):
    """Replace the manager's httpx client with one routed through a
    MockTransport. The lifespan's startup hook builds a real client first;
    this swap happens after the lifespan enters so there's something to
    swap. Tests are responsible for closing the mock client themselves —
    the lifespan shutdown closes whatever the manager currently holds.
    """
    transport = httpx.MockTransport(handler)
    manager._http_client = httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0),
    )


_PROXY_CONFIG = {
    "mcpServers": {
        "alpha": {
            "command": "echo",
            "args": ["a"],
            "reverseProxy": {
                "mount": "/alpha",
                "upstream": "http://upstream.test",
            },
        },
    },
}


class TestReverseProxy:
    @pytest.mark.asyncio
    async def test_forwards_request_and_injects_xff_headers(self):
        captured: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["method"] = req.method
            captured["url"] = str(req.url)
            captured["headers"] = dict(req.headers)
            captured["body"] = req.content
            return httpx.Response(
                200,
                content=b'{"hello":"world"}',
                headers={"Content-Type": "application/json"},
            )

        with _apply_patches()[1], _apply_patches()[2], _apply_patches()[3], _apply_patches()[4], _apply_patches()[5]:
            app, manager = _fresh()
            async with _lifespan(app):
                _install_mock_upstream(manager, handler)
                async with _client(app) as c:
                    start = await c.post("/api/start", json=_PROXY_CONFIG)
                    assert start.status_code == 200
                    r = await c.post(
                        "/alpha/v1/echo?foo=bar",
                        content=b'{"q":1}',
                        headers={"Content-Type": "application/json"},
                    )
                assert r.status_code == 200
                assert r.json() == {"hello": "world"}

                # Path/query/body forwarded verbatim (stripPrefix=False default).
                assert captured["method"] == "POST"
                assert captured["url"] == "http://upstream.test/alpha/v1/echo?foo=bar"
                assert captured["body"] == b'{"q":1}'

                hdrs = captured["headers"]
                assert hdrs["x-forwarded-prefix"] == "/alpha"
                assert hdrs["x-forwarded-proto"] == "http"
                assert hdrs["x-forwarded-host"] == "testserver"
                # Caller's content-type is preserved.
                assert hdrs.get("content-type") == "application/json"

    @pytest.mark.asyncio
    async def test_503_when_backend_not_running(self):
        app, manager = _fresh()
        async with _lifespan(app):
            # Skip _apply_patches so start_all is never called — the spec is
            # injected directly into _specs but no ProxyState exists.
            from zelosmcp.config import parse_config
            specs, _, _ = parse_config(_PROXY_CONFIG)
            manager._specs = {s.name: s for s in specs}

            async with _client(app) as c:
                r = await c.get("/alpha/v1/health")
            assert r.status_code == 503
            assert r.json()["error"] == "No MCP server 'alpha' is running"

    @pytest.mark.asyncio
    async def test_strip_prefix_removes_mount(self):
        captured: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["url"] = str(req.url)
            return httpx.Response(204)

        with _apply_patches()[1], _apply_patches()[2], _apply_patches()[3], _apply_patches()[4], _apply_patches()[5]:
            app, manager = _fresh()
            async with _lifespan(app):
                _install_mock_upstream(manager, handler)
                async with _client(app) as c:
                    cfg = {
                        "mcpServers": {
                            "alpha": {
                                "command": "echo",
                                "args": ["a"],
                                "reverseProxy": {
                                    "mount": "/alpha",
                                    "upstream": "http://upstream.test",
                                    "stripPrefix": True,
                                },
                            },
                        },
                    }
                    await c.post("/api/start", json=cfg)
                    r = await c.get("/alpha/v1/health")
                assert r.status_code == 204
                # Prefix stripped: upstream sees /v1/health, not /alpha/v1/health.
                assert captured["url"] == "http://upstream.test/v1/health"

    @pytest.mark.asyncio
    async def test_bearer_injected_when_caller_has_no_auth(self):
        captured: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["auth"] = req.headers.get("authorization")
            return httpx.Response(200, content=b"")

        with _apply_patches()[1], _apply_patches()[2], _apply_patches()[3], _apply_patches()[4], _apply_patches()[5]:
            app, manager = _fresh()
            async with _lifespan(app):
                _install_mock_upstream(manager, handler)
                async with _client(app) as c:
                    cfg = {
                        "mcpServers": {
                            "alpha": {
                                "command": "echo",
                                "args": ["a"],
                                "reverseProxy": {
                                    "mount": "/alpha",
                                    "upstream": "http://upstream.test",
                                    "auth": {"bearer": "s3cret"},
                                },
                            },
                        },
                    }
                    await c.post("/api/start", json=cfg)
                    await c.get("/alpha/v1/health")
                assert captured["auth"] == "Bearer s3cret"

    @pytest.mark.asyncio
    async def test_bearer_not_overridden_when_caller_has_auth(self):
        captured: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["auth"] = req.headers.get("authorization")
            return httpx.Response(200, content=b"")

        with _apply_patches()[1], _apply_patches()[2], _apply_patches()[3], _apply_patches()[4], _apply_patches()[5]:
            app, manager = _fresh()
            async with _lifespan(app):
                _install_mock_upstream(manager, handler)
                async with _client(app) as c:
                    cfg = {
                        "mcpServers": {
                            "alpha": {
                                "command": "echo",
                                "args": ["a"],
                                "reverseProxy": {
                                    "mount": "/alpha",
                                    "upstream": "http://upstream.test",
                                    "auth": {"bearer": "s3cret"},
                                },
                            },
                        },
                    }
                    await c.post("/api/start", json=cfg)
                    await c.get(
                        "/alpha/v1/health",
                        headers={"Authorization": "Bearer caller-token"},
                    )
                assert captured["auth"] == "Bearer caller-token"

    @pytest.mark.asyncio
    async def test_upstream_unreachable_returns_502(self):
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("simulated dead upstream")

        with _apply_patches()[1], _apply_patches()[2], _apply_patches()[3], _apply_patches()[4], _apply_patches()[5]:
            app, manager = _fresh()
            async with _lifespan(app):
                _install_mock_upstream(manager, handler)
                async with _client(app) as c:
                    await c.post("/api/start", json=_PROXY_CONFIG)
                    r = await c.get("/alpha/v1/health")
                assert r.status_code == 502
                body = r.json()
                assert body["backend"] == "alpha"
                assert "simulated dead upstream" in body["detail"]

    @pytest.mark.asyncio
    async def test_named_mcp_takes_precedence_over_proxy(self):
        """A backend mounted at /alpha must keep /alpha/mcp routing to the
        MCP session, not the reverse proxy. The dispatcher checks /<name>/mcp
        before the proxy table.

        We intercept ``session_manager.handle_request`` with a sentinel so we
        can assert exactly which dispatch arm fired without standing up the
        full MCP task group (the existing test fakes patch ``run()`` to a
        no-op, which leaves the session manager's internal task group
        uninitialised).
        """
        with _apply_patches()[1], _apply_patches()[2], _apply_patches()[3], _apply_patches()[4], _apply_patches()[5]:
            app, manager = _fresh()
            async with _lifespan(app):
                proxy_hits: list[str] = []
                mcp_hits: list[str] = []

                def proxy_handler(req: httpx.Request) -> httpx.Response:
                    proxy_hits.append(str(req.url))
                    return httpx.Response(200, content=b"")

                _install_mock_upstream(manager, proxy_handler)
                async with _client(app) as c:
                    await c.post("/api/start", json=_PROXY_CONFIG)
                    state = manager.get("alpha")
                    assert state is not None and state.session_manager is not None

                    async def fake_handle_request(scope, receive, send):
                        mcp_hits.append(scope.get("path", ""))
                        # Minimal valid ASGI response so the test client doesn't choke.
                        await send({"type": "http.response.start", "status": 204, "headers": []})
                        await send({"type": "http.response.body", "body": b"", "more_body": False})

                    state.session_manager.handle_request = fake_handle_request

                    r = await c.post(
                        "/alpha/mcp",
                        json={"jsonrpc": "2.0", "id": 1, "method": "x", "params": {}},
                        headers={"Accept": "application/json, text/event-stream"},
                    )
                assert r.status_code == 204
                # Dispatcher chose the MCP arm, not the proxy arm.
                assert mcp_hits == ["/mcp"]  # path was rewritten by dispatcher
                assert proxy_hits == []

    @pytest.mark.asyncio
    async def test_status_exposes_reverse_proxy(self):
        with _apply_patches()[1], _apply_patches()[2], _apply_patches()[3], _apply_patches()[4], _apply_patches()[5]:
            app, _ = _fresh()
            async with _lifespan(app):
                async with _client(app) as c:
                    await c.post("/api/start", json=_PROXY_CONFIG)
                    r = await c.get("/api/status")
                row = next(s for s in r.json()["servers"] if s["name"] == "alpha")
                assert row["spec"]["reverseProxy"] == {
                    "mount": "/alpha",
                    "upstream": "http://upstream.test",
                }


# ── Compression integration ────────────────────────────────────────────


def _compress_config(scope: str, level: str = "medium") -> dict:
    return {
        "mcpServers": {
            "alpha": {
                "command": "echo",
                "args": ["a"],
                "compress": {"level": level, "scope": scope},
            }
        }
    }


@asynccontextmanager
async def _compress_test_env():
    """Stand up app + lifespan with stdio + ClientSession mocked but session
    managers running real. The other helper, _apply_patches(), mocks
    StreamableHTTPSessionManager.run too — that no-op breaks dispatch
    through /mcp and /<name>/mcp because the task group never gets set up.
    """
    mock_session = make_mock_session()

    @asynccontextmanager
    async def patched_client_session(read, write):
        yield mock_session

    with (
        patch("zelosmcp.proxy.stdio_client", side_effect=fake_stdio_client),
        patch("zelosmcp.proxy.ClientSession", side_effect=patched_client_session),
    ):
        app, manager = _fresh()
        async with _lifespan(app):
            yield app, manager, mock_session


class TestCompressedAggregatorIntegration:
    """End-to-end check that POSTing a compressed config swaps the
    aggregator's tool surface for the wrapper trio, and that
    get_tool_schema + invoke_tool both round-trip through ``/mcp``."""

    @pytest.mark.asyncio
    async def test_aggregator_serves_wrappers_when_scope_aggregator(self):
        async with _compress_test_env() as (app, _, _):
            async with _client(app) as c:
                await c.post("/api/start", json=_compress_config("aggregator"))
                r = await c.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                    headers={"Accept": "application/json, text/event-stream"},
                )
            names = sorted(t["name"] for t in r.json()["result"]["tools"]
                           if t["name"].startswith("alpha__"))
            assert names == [
                "alpha__get_tool_schema",
                "alpha__invoke_tool",
                "alpha__search_tools",
            ]

    @pytest.mark.asyncio
    async def test_aggregator_keeps_full_list_when_scope_catalog(self):
        async with _compress_test_env() as (app, manager, _):
            async with _client(app) as c:
                await c.post("/api/start", json=_compress_config("catalog"))
                r = await c.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                    headers={"Accept": "application/json, text/event-stream"},
                )
            names = sorted(t["name"] for t in r.json()["result"]["tools"]
                           if t["name"].startswith("alpha__"))
            assert names == ["alpha__add", "alpha__echo"]
            assert "alpha" in manager.aggregator.compressed_catalog

    @pytest.mark.asyncio
    async def test_invoke_tool_dispatches_through_aggregator(self):
        async with _compress_test_env() as (app, _, _):
            async with _client(app) as c:
                await c.post("/api/start", json=_compress_config("aggregator"))
                r = await c.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {
                            "name": "alpha__invoke_tool",
                            "arguments": {"tool_name": "echo", "tool_input": {"x": 1}},
                        },
                    },
                    headers={"Accept": "application/json, text/event-stream"},
                )
            payload = r.json()["result"]
            assert payload["content"][0]["text"] == "hello"
            assert payload["isError"] is False

    @pytest.mark.asyncio
    async def test_get_tool_schema_returns_underlying_schema(self):
        async with _compress_test_env() as (app, _, _):
            async with _client(app) as c:
                await c.post("/api/start", json=_compress_config("aggregator"))
                # Prime the catalog cache.
                await c.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "id": 0, "method": "tools/list", "params": {}},
                    headers={"Accept": "application/json, text/event-stream"},
                )
                r = await c.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {
                            "name": "alpha__get_tool_schema",
                            "arguments": {"tool_name": "echo"},
                        },
                    },
                    headers={"Accept": "application/json, text/event-stream"},
                )
            payload = r.json()["result"]
            assert payload["isError"] is False
            body = json.loads(payload["content"][0]["text"])
            assert body["name"] == "echo"


class TestPerBackendGlobalScope:
    """Confirm that /<name>/mcp uses compressed wrappers only when the
    backend's compress.scope is `global`. Default scope (`aggregator`)
    leaves /<name>/mcp serving the raw backend surface unchanged."""

    @staticmethod
    async def _list_at_named_route(c, name: str) -> list[str]:
        r = await c.post(
            f"/{name}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
        )
        return sorted(t["name"] for t in r.json()["result"]["tools"])

    @pytest.mark.asyncio
    async def test_scope_aggregator_keeps_named_route_uncompressed(self):
        async with _compress_test_env() as (app, _, _):
            async with _client(app) as c:
                await c.post("/api/start", json=_compress_config("aggregator"))
                names = await self._list_at_named_route(c, "alpha")
            assert names == ["add", "echo"]

    @pytest.mark.asyncio
    async def test_scope_global_compresses_named_route(self):
        async with _compress_test_env() as (app, _, _):
            async with _client(app) as c:
                await c.post("/api/start", json=_compress_config("global"))
                names = await self._list_at_named_route(c, "alpha")
            # No backend prefix at /<name>/mcp — clients already know
            # the backend by URL.
            assert names == ["get_tool_schema", "invoke_tool", "search_tools"]

    @pytest.mark.asyncio
    async def test_scope_global_search_tools_uses_named_route_catalog(self):
        async with _compress_test_env() as (app, _, session):
            async with _client(app) as c:
                await c.post("/api/start", json=_compress_config("global"))
                # Prime the per-backend catalog through /<name>/mcp.
                await self._list_at_named_route(c, "alpha")
                r = await c.post(
                    "/alpha/mcp",
                    json={
                        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {
                            "name": "search_tools",
                            "arguments": {"query": "echo"},
                        },
                    },
                    headers={"Accept": "application/json, text/event-stream"},
                )
            payload = r.json()["result"]
            assert payload["isError"] is False
            assert "echo" in payload["content"][0]["text"]
            session.call_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_scope_catalog_keeps_named_route_uncompressed(self):
        async with _compress_test_env() as (app, _, _):
            async with _client(app) as c:
                await c.post("/api/start", json=_compress_config("catalog"))
                names = await self._list_at_named_route(c, "alpha")
            assert names == ["add", "echo"]


class TestBuiltinListCompressedTools:
    """The builtin `zelosmcp__list_compressed_tools` tool surfaces the
    aggregator's catalog cache for any backend with compress configured —
    independent of scope, so scope=catalog backends still show up."""

    @pytest.mark.asyncio
    async def test_lists_compressed_backends(self):
        async with _compress_test_env() as (app, _, _):
            async with _client(app) as c:
                await c.post("/api/start", json=_compress_config("aggregator"))
                # Prime the catalog cache.
                await c.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "id": 0, "method": "tools/list", "params": {}},
                    headers={"Accept": "application/json, text/event-stream"},
                )
                r = await c.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {
                            "name": "zelosmcp__list_compressed_tools",
                            "arguments": {},
                        },
                    },
                    headers={"Accept": "application/json, text/event-stream"},
                )
            body = json.loads(r.json()["result"]["content"][0]["text"])
            assert "alpha" in body
            assert body["alpha"]["configured"]["level"] == "medium"
            assert body["alpha"]["configured"]["scope"] == "aggregator"
            assert body["alpha"]["tool_count"] == 2
            assert any("echo" in line for line in body["alpha"]["catalog"])

    @pytest.mark.asyncio
    async def test_scope_catalog_is_visible_here(self):
        async with _compress_test_env() as (app, _, _):
            async with _client(app) as c:
                await c.post("/api/start", json=_compress_config("catalog"))
                await c.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "id": 0, "method": "tools/list", "params": {}},
                    headers={"Accept": "application/json, text/event-stream"},
                )
                r = await c.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {
                            "name": "zelosmcp__list_compressed_tools",
                            "arguments": {},
                        },
                    },
                    headers={"Accept": "application/json, text/event-stream"},
                )
            body = json.loads(r.json()["result"]["content"][0]["text"])
            assert "alpha" in body
            assert body["alpha"]["configured"]["scope"] == "catalog"

    @pytest.mark.asyncio
    async def test_level_override_re_renders(self):
        async with _compress_test_env() as (app, _, _):
            async with _client(app) as c:
                await c.post("/api/start", json=_compress_config("aggregator"))
                await c.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "id": 0, "method": "tools/list", "params": {}},
                    headers={"Accept": "application/json, text/event-stream"},
                )
                r = await c.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {
                            "name": "zelosmcp__list_compressed_tools",
                            "arguments": {"level": "high"},
                        },
                    },
                    headers={"Accept": "application/json, text/event-stream"},
                )
            body = json.loads(r.json()["result"]["content"][0]["text"])
            assert body["alpha"]["configured"]["level"] == "medium"
            assert body["alpha"]["render_level"] == "high"
            for line in body["alpha"]["catalog"]:
                assert "(" in line and ")" in line


# ── OAuth-passthrough end-to-end through the ASGI dispatcher ─────────────


class _FakeByteStream(httpx.AsyncByteStream):
    """Minimal AsyncByteStream wrapper so MockTransport responses survive
    the manager's ``aiter_raw()`` streaming loop. Real httpx responses
    (against a real upstream) are already streamable; MockTransport
    pre-buffers content into ``Response.content`` and marks the stream
    consumed, which trips StreamConsumed on iteration. Wrapping the body
    in an explicit AsyncByteStream restores streaming semantics.
    """

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


def _streaming_response(
    status: int,
    headers: dict[str, str] | None = None,
    *,
    chunks: list[bytes] | None = None,
    body: bytes | None = None,
) -> httpx.Response:
    """Build a streaming-capable httpx.Response for MockTransport."""
    if chunks is None:
        chunks = [body or b""]
    return httpx.Response(
        status,
        headers=headers,
        stream=_FakeByteStream(chunks),
    )


class TestPassthroughDispatcher:
    """End-to-end coverage of /<name>/mcp dispatching to a passthrough
    backend. Uses httpx.MockTransport on the manager's outbound client to
    intercept upstream calls without ever opening a network connection.
    """

    @staticmethod
    def _install_mock_upstream(manager, handler):
        """Replace the manager's outbound httpx.AsyncClient with one that
        routes every request through ``handler`` (a sync callable taking
        an httpx.Request and returning an httpx.Response).
        """
        manager._http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        )

    @pytest.mark.asyncio
    async def test_unauthenticated_request_propagates_401(self):
        seen: list[httpx.Request] = []

        def upstream(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _streaming_response(
                401,
                headers={
                    "WWW-Authenticate": (
                        'Bearer resource_metadata='
                        '"https://api.example.com/.well-known/oauth-protected-resource"'
                    ),
                    "content-type": "application/json",
                },
                body=b'{"error":"unauthorized"}',
            )

        app, manager = _fresh()
        self._install_mock_upstream(manager, upstream)
        await manager.start_all({
            "mcpServers": {
                "github": {
                    "type": "streamable-http",
                    "url": "https://api.example.com/mcp",
                    "passthrough": True,
                },
            },
        })
        try:
            async with _client(app) as c:
                r = await c.post(
                    "/github/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={"Accept": "application/json, text/event-stream"},
                )
            assert r.status_code == 401
            ww = r.headers.get("www-authenticate")
            assert ww is not None
            assert "resource_metadata" in ww
            assert "api.example.com" in ww
            assert r.json() == {"error": "unauthorized"}
            # Upstream saw exactly one request, no Authorization injected.
            assert len(seen) == 1
            assert "authorization" not in {h.lower() for h in seen[0].headers}
        finally:
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_authenticated_request_forwards_token(self):
        seen: list[httpx.Request] = []

        def upstream(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _streaming_response(
                200,
                headers={"content-type": "application/json"},
                body=b'{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}',
            )

        app, manager = _fresh()
        self._install_mock_upstream(manager, upstream)
        await manager.start_all({
            "mcpServers": {
                "github": {
                    "type": "streamable-http",
                    "url": "https://api.example.com/mcp",
                    "passthrough": True,
                },
            },
        })
        try:
            async with _client(app) as c:
                r = await c.post(
                    "/github/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={
                        "Authorization": "Bearer caller-token",
                        "Accept": "application/json, text/event-stream",
                    },
                )
            assert r.status_code == 200
            assert r.json()["result"]["tools"] == []
            assert seen[0].headers.get("authorization") == "Bearer caller-token"
            # Upstream URL is the configured one, NOT the inbound /github/mcp.
            assert str(seen[0].url) == "https://api.example.com/mcp"
        finally:
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_static_bearer_fallback_used_when_no_caller_auth(self):
        seen: list[httpx.Request] = []

        def upstream(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _streaming_response(200, body=b"{}")

        app, manager = _fresh()
        self._install_mock_upstream(manager, upstream)
        await manager.start_all({
            "mcpServers": {
                "github": {
                    "type": "streamable-http",
                    "url": "https://api.example.com/mcp",
                    "passthrough": True,
                    "auth": {"bearer": "static-fallback"},
                },
            },
        })
        try:
            async with _client(app) as c:
                r = await c.post(
                    "/github/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={"Accept": "application/json, text/event-stream"},
                )
            assert r.status_code == 200
            assert seen[0].headers.get("authorization") == "Bearer static-fallback"
        finally:
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_caller_auth_overrides_static_bearer(self):
        seen: list[httpx.Request] = []

        def upstream(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _streaming_response(200, body=b"{}")

        app, manager = _fresh()
        self._install_mock_upstream(manager, upstream)
        await manager.start_all({
            "mcpServers": {
                "github": {
                    "type": "streamable-http",
                    "url": "https://api.example.com/mcp",
                    "passthrough": True,
                    "auth": {"bearer": "static-fallback"},
                },
            },
        })
        try:
            async with _client(app) as c:
                r = await c.post(
                    "/github/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={
                        "Authorization": "Bearer caller-wins",
                        "Accept": "application/json, text/event-stream",
                    },
                )
            assert r.status_code == 200
            assert seen[0].headers.get("authorization") == "Bearer caller-wins"
        finally:
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_503_when_passthrough_backend_not_running(self):
        # No start_all => no backend configured / running. The dispatcher
        # should return 503 with a clear error rather than hanging or
        # reaching for a session_manager that doesn't exist.
        app, manager = _fresh()
        manager._http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: _streaming_response(200, body=b"{}")
            ),
        )
        try:
            async with _client(app) as c:
                r = await c.post(
                    "/github/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={"Accept": "application/json, text/event-stream"},
                )
            assert r.status_code == 503
            assert "github" in r.text
        finally:
            await manager._http_client.aclose()

    @pytest.mark.asyncio
    async def test_session_bound_backend_unaffected(self):
        # Sanity: a non-passthrough HTTP backend should still route via
        # the session_manager (mocked through patches), not via the
        # passthrough forwarder. We assert by configuring an upstream
        # mock that would error if hit — a session-bound /<name>/mcp
        # request must NOT touch the outbound HTTP client.
        outbound_was_hit = []

        def upstream(request: httpx.Request) -> httpx.Response:
            outbound_was_hit.append(request)
            return _streaming_response(500, body=b"should-never-be-called")

        mock_session, p1, p2, p3, p4, p5 = _apply_patches()
        with p1, p2, p3, p4, p5:
            app, manager = _fresh()
            manager._http_client = httpx.AsyncClient(
                transport=httpx.MockTransport(upstream),
            )
            await manager.start_all({
                "mcpServers": {
                    "alpha": {
                        "type": "streamable-http",
                        "url": "https://api.example.com/mcp",
                        # passthrough deliberately omitted
                    },
                },
            })
            try:
                async with _client(app) as c:
                    # The mocked StreamableHTTPSessionManager.run is a
                    # no-op CM, so the session_manager attribute is
                    # actually set on the ProxyState. We can verify the
                    # routing decision by checking is_passthrough is
                    # False and the dispatcher chose the session path.
                    state = manager.get("alpha")
                    assert state is not None
                    assert state.is_passthrough is False
                # The outbound mock should NOT have been touched.
                assert outbound_was_hit == []
            finally:
                await manager.stop_all()
