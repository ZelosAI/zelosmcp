from __future__ import annotations

import contextlib
import logging
import os

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.schemas import SchemaGenerator

from zelosmcp.manager import ProxyManager
from zelosmcp.routes import (
    assets as asset_routes,
    auth as auth_routes,
    docs_view as docs_view_routes,
    health as health_routes,
    pages as pages_routes,
    repos as repos_routes,
    servers as servers_routes,
    streaming as streaming_routes,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


SCHEMA = SchemaGenerator(
    {
        "openapi": "3.0.3",
        "info": {
            "title": "zelosMCP",
            "version": "0.3.0",
            "description": (
                "Wrap one or more MCP servers and re-expose them on stable local URLs.\n\n"
                "- Each configured server is mounted at `/<name>/mcp` (raw passthrough — "
                "tools, resources, and prompts unchanged).\n"
                "- `/mcp` is an aggregator that fans tools, prompts, and resources "
                "across every running backend. Tool and prompt names are surfaced as "
                "`<server>__<original>` (double underscore). Resource URIs are kept "
                "verbatim; reads are routed to the originating backend via a "
                "URI->backend cache populated from `resources/list`, with a fan-out "
                "fallback for URIs not previously listed.\n"
                "- `/zelosmcp/mcp` is the always-on built-in MCP that exposes "
                "self-introspection and Cursor-rule-generation tools "
                "(`zelosmcp__*` at /mcp). It survives configuration reloads."
            ),
        },
        "servers": [{"url": "http://localhost:8000"}],
        "tags": [
            {"name": "lifecycle", "description": "Start/stop proxied MCP servers"},
            {"name": "introspection", "description": "Inspect status and logs"},
            {"name": "mcp", "description": "Streamable-HTTP MCP endpoints"},
        ],
    },
)


_SWAGGER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>zelosMCP API</title>
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    window.onload = () => {
      window.ui = SwaggerUIBundle({
        url: "/openapi.json",
        dom_id: "#swagger-ui",
        deepLinking: true,
        layout: "BaseLayout",
      });
    };
  </script>
</body>
</html>
"""


_REDOC_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>zelosMCP API</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>body { margin: 0; padding: 0; }</style>
</head>
<body>
  <redoc spec-url="/openapi.json"></redoc>
  <script src="https://cdn.redocly.com/redoc/latest/bundles/redoc.standalone.js"></script>
</body>
</html>
"""


# Default path for the auth-providers config when ``ZELOSMCP_AUTH_PROVIDERS_FILE``
# isn't set. Container deploys override this to ``/etc/zelosmcp/auth-providers.json``
# (mounted from a Kubernetes Secret); local dev points at the repo path.
_DEFAULT_AUTH_PROVIDERS_PATH = "configs/auth-providers.json"


async def _autoload_auth_providers(manager) -> None:
    """Load ``configs/auth-providers.json`` (or whatever
    ``ZELOSMCP_AUTH_PROVIDERS_FILE`` points at) into the manager's
    auth registry at startup.

    Missing file is logged and skipped — a deployment with no
    providers is a valid (legacy-passthrough-only) configuration.
    Malformed file logs the error but doesn't crash the app; the
    registry stays empty and any backend referencing a provider
    will fail at /api/start time with a clear message.
    """
    import json as _json

    log = logging.getLogger("zelosmcp")
    path = os.environ.get(
        "ZELOSMCP_AUTH_PROVIDERS_FILE", _DEFAULT_AUTH_PROVIDERS_PATH
    )
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = _json.load(f)
    except FileNotFoundError:
        log.info(
            "auth-providers config not found at %s; starting with empty registry",
            path,
        )
        return
    except (OSError, _json.JSONDecodeError) as exc:
        log.error(
            "auth-providers config %s failed to load: %s", path, exc
        )
        return

    try:
        result = await manager.start_auth_providers(payload)
    except Exception as exc:
        log.error(
            "auth-providers config %s failed to register: %s", path, exc
        )
        return
    log.info(
        "auth-providers loaded from %s: %s", path, result.get("providers", {})
    )


def create_app(manager: ProxyManager | None = None):
    """Build the ASGI application. Accepts an optional ProxyManager for testing."""
    if manager is None:
        manager = ProxyManager()






    @contextlib.asynccontextmanager
    async def lifespan(app):
        # Start the always-on builtin MCP before serving any traffic so
        # /zelosmcp/mcp answers immediately and the aggregator can already
        # fan tools/list out to the builtin's in-memory client session.
        try:
            await manager.start_builtin()
        except Exception as exc:  # never fail the whole app on builtin startup
            logging.getLogger("zelosmcp").error(
                "builtin failed to start: %s", exc, exc_info=True
            )
        # Bring up the always-on data-path MCP (/zelos/mcp): sync subagents
        # (#24) + async task (#25). Non-fatal on failure — its tools become
        # unreachable but the rest of zelosmcp still serves.
        try:
            await manager.start_datapath()
        except Exception as exc:
            logging.getLogger("zelosmcp").error(
                "data-path server failed to start: %s", exc, exc_info=True
            )
        # Bring up the reverse-proxy httpx client so the dispatcher can
        # forward requests as soon as the first backend with a configured
        # reverseProxy starts.
        try:
            await manager.start_http_client()
        except Exception as exc:
            logging.getLogger("zelosmcp").error(
                "reverse-proxy client failed to start: %s", exc, exc_info=True
            )
        # Open the encrypted auth store before any provider tries to read
        # / write user tokens. Failure is logged but non-fatal — the app
        # still boots; OAuth providers just won't work until the store is
        # available.
        try:
            await manager.start_auth_store()
        except Exception as exc:
            logging.getLogger("zelosmcp").error(
                "auth store failed to start: %s", exc, exc_info=True
            )
        # Auto-load the auth-providers config from disk before serving
        # any traffic so backend specs that reference providers can
        # resolve at /api/start time. Path follows the same priority
        # order as ZELOSMCP_CONFIG: explicit env > default. Missing
        # file is non-fatal (deployment with no providers is valid);
        # malformed file fails the lifespan so misconfig surfaces at
        # boot rather than the first auth attempt.
        await _autoload_auth_providers(manager)
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                await manager.stop_datapath()
            with contextlib.suppress(Exception):
                await manager.stop_builtin()
            with contextlib.suppress(Exception):
                await manager.stop_all()
            with contextlib.suppress(Exception):
                await manager.stop_auth_store()
            with contextlib.suppress(Exception):
                await manager.stop_http_client()

    _starlette = Starlette(
        lifespan=lifespan,
        routes=[
            *health_routes.make_routes(manager),
            *pages_routes.make_routes(manager, SCHEMA, _SWAGGER_HTML, _REDOC_HTML),
            *servers_routes.make_routes(manager),
            *docs_view_routes.make_routes(manager),
            *streaming_routes.make_routes(manager),
            *repos_routes.make_routes(manager),
            *asset_routes.make_routes(manager),
            *auth_routes.make_routes(manager),
        ],
    )

    async def _handle_aggregate_with_challenge(
        session_manager,
        scope: dict,
        receive,
        send,
        challenge_cls,
    ) -> None:
        """Wrap aggregator ``session_manager.handle_request`` so a
        :class:`PassthroughChallengeError` raised by an aggregator
        handler surfaces as a transport-level 401 + WWW-Authenticate
        instead of getting buried in a JSON-RPC error envelope.

        Why we use a side-channel ContextVar (``pending_challenge``)
        instead of catching exceptions: MCP's lowlevel ``Server``
        catches *any* exception from a handler and serialises it as a
        JSON-RPC error response (HTTP 200). A plain ``raise`` would
        therefore never surface the challenge to the HTTP layer.
        The aggregator handlers set the ContextVar before returning;
        we check it here after ``handle_request`` returns and rewrite
        the response if set.

        We buffer ALL response messages until ``handle_request``
        completes so we can swap the entire response (status + headers
        + body) in one shot.
        """
        from zelosmcp.passthrough_pool import pending_challenge as _pending

        # Bind a fresh mutable list as the per-request signal slot.
        # The list is shared by reference into child tasks the SDK
        # spawns to run handlers, so a handler appending to the list
        # is visible to us after ``handle_request`` returns.
        challenge_box: list = []
        token = _pending.set(challenge_box)

        buffered: list[dict] = []

        async def buffered_send(message: dict) -> None:
            buffered.append(message)

        challenge: BaseException | None = None
        try:
            await session_manager.handle_request(scope, receive, buffered_send)
        except challenge_cls as exc:
            challenge = exc
        finally:
            # If a handler signalled a challenge via the list, prefer
            # the first one (closest to the failed call). A direct
            # exception from handle_request also wins over signals.
            if challenge is None and challenge_box:
                challenge = challenge_box[0]
            _pending.reset(token)

        if challenge is not None:
            ww = getattr(challenge, "www_authenticate", None) or "Bearer"
            status = getattr(challenge, "status", 401) or 401
            backend = getattr(challenge, "backend", "unknown")
            await send({
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", ww.encode("latin-1", errors="replace")),
                ],
            })
            import json as _json

            await send({
                "type": "http.response.body",
                "body": _json.dumps({
                    "error": "authentication_required",
                    "backend": backend,
                }).encode("utf-8"),
                "more_body": False,
            })
            return

        # No challenge: replay the buffered messages in order.
        for msg in buffered:
            await send(msg)

    async def asgi_app(scope, receive, send) -> None:
        """Dispatch /<name>/mcp, /mcp, and any backend's reverseProxy mount
        before Starlette's router."""
        if scope["type"] == "http":
            path = scope["path"]
            normalized = path.rstrip("/") or "/"

            target = None
            target_label = None

            if normalized == "/mcp":
                target = manager.aggregator
                target_label = "aggregate"
            elif normalized.endswith("/mcp"):
                # /<name>/mcp — exactly two slashes after stripping trailing.
                segments = [s for s in normalized.split("/") if s]
                if len(segments) == 2 and segments[1] == "mcp":
                    name = segments[0]
                    target = manager.get(name)
                    target_label = name
                else:
                    target = None
                    target_label = None

            if target_label is not None:
                # OAuth-passthrough backends have no zelosMCP-owned
                # session_manager; route them through the streaming HTTP
                # forwarder so the client's OAuth dance flows directly to
                # the upstream issuer. Only applies to /<name>/mcp — the
                # /mcp aggregator handles passthrough internally via the
                # session pool (Phase 2B).
                if (
                    target is not None
                    and target_label != "aggregate"
                    and getattr(target, "is_passthrough", False)
                ):
                    if not getattr(target, "running", False):
                        resp = JSONResponse(
                            {"error": f"No MCP server '{target_label}' is running"},
                            status_code=503,
                            headers={"Retry-After": "2"},
                        )
                        return await resp(scope, receive, send)
                    spec = manager.get_spec(target_label)
                    if spec is None or not spec.passthrough:
                        # Inconsistent state — state says passthrough but
                        # spec disagrees. Fail loudly rather than silently
                        # routing through the wrong path.
                        resp = JSONResponse(
                            {
                                "error": (
                                    f"backend '{target_label}' is in passthrough "
                                    "state but has no matching ServerSpec"
                                )
                            },
                            status_code=500,
                        )
                        return await resp(scope, receive, send)
                    return await manager.proxy_mcp_request(spec, scope, receive, send)

                if target is not None and getattr(target, "session_manager", None) is not None:
                    # Strip the routing prefix so the session manager sees a
                    # path it expects (e.g. "/mcp" or "").
                    forwarded = dict(scope)
                    forwarded["path"] = "/mcp"
                    forwarded["raw_path"] = b"/mcp"
                    # Make the inbound HTTP Authorization header readable
                    # from inside MCP handlers via the ContextVar set
                    # below. The aggregator uses this to route passthrough
                    # backend calls through their per-token session pool;
                    # all other backends ignore it.
                    from zelosmcp.passthrough_pool import (
                        PassthroughChallengeError,
                        inbound_authorization,
                    )

                    auth_value: str | None = None
                    for k, v in scope.get("headers", []):
                        if k.lower() == b"authorization":
                            try:
                                auth_value = v.decode("latin-1")
                            except Exception:
                                auth_value = None
                            break
                    auth_token = inbound_authorization.set(auth_value)
                    # Bind the gateway-propagated caller identity
                    # (X-Zelos-Subject / X-Zelos-Scopes) so the data-path
                    # tools (#24, #25) can mint per-invocation downstream
                    # bearer tokens (#26) for the right subject. Anonymous
                    # when the headers are absent (direct dev call).
                    from zelosmcp.auth.identity import (
                        current_identity,
                        identity_from_scope_headers,
                    )

                    identity = identity_from_scope_headers(scope.get("headers", []))
                    identity_token = current_identity.set(identity)
                    try:
                        # Phase 2C: the middleware around session_manager
                        # converts a PassthroughChallengeError raised
                        # from inside an aggregator handler into a 401
                        # + WWW-Authenticate response. For the per-
                        # backend / non-aggregator path the session
                        # manager runs handlers as today.
                        if target_label == "aggregate":
                            return await _handle_aggregate_with_challenge(
                                target.session_manager,
                                forwarded,
                                receive,
                                send,
                                PassthroughChallengeError,
                            )
                        return await target.session_manager.handle_request(
                            forwarded, receive, send
                        )
                    finally:
                        inbound_authorization.reset(auth_token)
                        current_identity.reset(identity_token)
                if target_label == "aggregate":
                    msg = "No MCP servers are running"
                else:
                    msg = f"No MCP server '{target_label}' is running"
                resp = JSONResponse(
                    {"error": msg},
                    status_code=503,
                    headers={"Retry-After": "2"},
                )
                return await resp(scope, receive, send)

            # Reverse-proxy dispatch: a backend may declare a
            # `reverseProxy.mount` so its HTTP sidecar is reachable
            # under zelosMCP's port. Match on the original (un-stripped)
            # path since mounts are absolute. /<name>/mcp wins above so
            # a backend named `pincher` mounted at `/pincher` keeps
            # `/pincher/mcp` for MCP and routes `/pincher/v1/...` here.
            match = manager.find_reverse_proxy(path)
            if match is not None:
                spec, state = match
                running = (
                    state is not None
                    and getattr(state, "running", False)
                )
                if not running:
                    resp = JSONResponse(
                        {"error": f"No MCP server '{spec.name}' is running"},
                        status_code=503,
                        headers={"Retry-After": "2"},
                    )
                    return await resp(scope, receive, send)
                return await manager.proxy_request(spec, scope, receive, send)

        await _starlette(scope, receive, send)

    asgi_app._manager = manager  # type: ignore[attr-defined]
    asgi_app.routes = _starlette.routes  # type: ignore[attr-defined]
    return asgi_app


app = create_app()


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    uvicorn.run(app, host=host, port=port, log_level="info", timeout_graceful_shutdown=2)
