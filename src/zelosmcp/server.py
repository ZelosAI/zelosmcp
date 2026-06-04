"""Data-path MCP server: sync subagents (#24) + async task (#25).

A second always-on, in-process MCP server — structurally a
:class:`zelosmcp.proxy.ProxyState` look-alike like
:class:`zelosmcp.builtin.BuiltinServer` — that exposes the v0.3 data-path
tools and is mounted at ``/zelos/mcp``.

Why a separate server rather than folding the tools into the builtin:

* The builtin (``/zelosmcp/mcp``) is the suite-introspection surface and its
  tool roster is contract-pinned (``len(_TOOLS) == 8``). The data-path tools
  are a different concern (they reach out to the broker / backplane), so they
  get their own mount.
* It is registered with ``client_session = None`` so the ``/mcp`` aggregator
  and ``GET /api/catalog`` deliberately *skip* it — the data-path tools are
  invoked directly at ``/zelos/mcp``, not fanned into the aggregate surface.

Tools (built from :mod:`zelosmcp.tools`):

* one per subagent (``plan`` / ``explore`` / ``general_purpose``) — open a
  broker sync channel and stream turns (#24).
* ``submit_inference_task`` — publish a backplane request envelope (#25).

Per-invocation broker / backplane bearer tokens are issued by the auth layer
(#26); the caller identity is read from
:data:`zelosmcp.auth.identity.current_identity`, which the ASGI dispatcher
binds from the gateway's ``X-Zelos-*`` headers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import AsyncExitStack, suppress
from typing import TYPE_CHECKING, Any

from mcp.server.lowlevel.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.exceptions import McpError
from mcp.types import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    ContentBlock,
    ErrorData,
    TextContent,
    Tool,
)

from zelosmcp.backplane.publisher import BACKPLANE_URL_ENV, BackplanePublisher
from zelosmcp.broker.client import BROKER_URL_ENV, BrokerClient
from zelosmcp.loader import BundleManifestError
from zelosmcp.tools.async_task import (
    ASYNC_TASK_TOOL,
    AsyncTaskDeps,
    submit_inference_task,
)
from zelosmcp.tools.sync_subagent import (
    SubagentDeps,
    run_sync_subagent,
    subagent_tool_specs,
)

if TYPE_CHECKING:  # pragma: no cover
    from zelosmcp.loader import SubagentMeta
    from zelosmcp.manager import ProxyManager

logger = logging.getLogger("zelosmcp")

NAME = "zelos"


def build_tools() -> list[Tool]:
    """Build the data-path tool list: one per subagent + the async task tool."""
    tools: list[Tool] = []
    for meta in subagent_tool_specs():
        tools.append(
            Tool(
                name=meta.tool_name,
                description=meta.description,
                inputSchema={
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "The instruction / question for the subagent.",
                        },
                        "workspace_path": {
                            "type": "string",
                            "description": (
                                "Workspace path to share with the subagent so "
                                "it can mount your files (optional)."
                            ),
                        },
                    },
                    "required": ["prompt"],
                    "additionalProperties": False,
                },
            )
        )
    tools.append(Tool(**ASYNC_TASK_TOOL))
    return tools


def _text(payload: str) -> list[ContentBlock]:
    return [TextContent(type="text", text=payload)]


def _json_text(obj: Any) -> list[ContentBlock]:
    return _text(json.dumps(obj, indent=2, default=str))


class DataPathServer:
    """Always-on, in-process MCP server for the v0.3 data-path tools.

    Shape-compatible with :class:`zelosmcp.proxy.ProxyState` so the ASGI
    dispatcher routes ``/zelos/mcp`` to it, but with ``client_session = None``
    so the aggregator skips it.
    """

    name = NAME

    def __init__(self, manager: ProxyManager) -> None:
        self.manager = manager
        self.session_manager: StreamableHTTPSessionManager | None = None
        # Deliberately None: keeps the data-path server OUT of the /mcp
        # aggregator and /api/catalog (both require client_session).
        self.client_session = None
        self.running: bool = False
        self.error: str | None = None
        self.backend_info: dict[str, Any] = {"transport": "builtin"}
        self.is_passthrough: bool = False
        self._log_subscribers: list[asyncio.Queue[str]] = []
        self._task: asyncio.Task | None = None
        self._ready: asyncio.Event = asyncio.Event()
        self._startup_error: BaseException | None = None
        # Lazily-built downstream clients, reused across invocations.
        self._broker: BrokerClient | None = None
        self._publisher: BackplanePublisher | None = None
        self._tools: list[Tool] = build_tools()

    # ── Log plumbing (mirrors ProxyState's API) ────────────────────────

    def _emit_log(self, message: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] [{self.name}] {message}"
        logger.info("[%s] %s", self.name, message)
        for q in list(self._log_subscribers):
            with suppress(asyncio.QueueFull):
                q.put_nowait(line)

    def subscribe_logs(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        self._log_subscribers.append(q)
        return q

    def unsubscribe_logs(self, q: asyncio.Queue[str]) -> None:
        with suppress(ValueError):
            self._log_subscribers.remove(q)

    # ── Downstream client accessors ────────────────────────────────────

    def _broker_client(self) -> BrokerClient:
        if self._broker is None:
            if not os.environ.get(BROKER_URL_ENV):
                raise McpError(
                    ErrorData(
                        code=INTERNAL_ERROR,
                        message=(
                            f"{BROKER_URL_ENV} is not configured; the broker "
                            "data path is unavailable"
                        ),
                    )
                )
            self._broker = BrokerClient()
        return self._broker

    def _backplane_publisher(self) -> BackplanePublisher:
        if self._publisher is None:
            if not os.environ.get(BACKPLANE_URL_ENV):
                raise McpError(
                    ErrorData(
                        code=INTERNAL_ERROR,
                        message=(
                            f"{BACKPLANE_URL_ENV} is not configured; the "
                            "backplane data path is unavailable"
                        ),
                    )
                )
            self._publisher = BackplanePublisher()
        return self._publisher

    # ── Tool dispatch ──────────────────────────────────────────────────

    async def _call_subagent(
        self, meta: SubagentMeta, arguments: dict[str, Any]
    ) -> list[ContentBlock]:
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            raise McpError(
                ErrorData(code=INVALID_PARAMS, message="`prompt` is required")
            )
        workspace_path = arguments.get("workspace_path")
        deps = SubagentDeps(broker=self._broker_client())
        try:
            result = await run_sync_subagent(
                meta,
                prompt=prompt,
                deps=deps,
                workspace_path=workspace_path,
            )
        except McpError:
            raise
        except BundleManifestError as exc:
            # A malformed / dangling artifact bundle manifest (#27) is a config
            # error, not an internal fault — surface it as INVALID_PARAMS with
            # the readable message so the operator can fix the manifest.
            raise McpError(
                ErrorData(code=INVALID_PARAMS, message=str(exc))
            ) from exc
        except Exception as exc:  # noqa: BLE001 - surface as MCP error
            raise McpError(
                ErrorData(code=INTERNAL_ERROR, message=str(exc))
            ) from exc
        return _json_text(
            {
                "session_id": result.session_id,
                "subagent": result.subagent,
                "transcript": result.transcript,
                "usage": result.usage,
                "frames": result.frames,
                "bundle": result.bundle,
            }
        )

    async def _call_submit_task(
        self, arguments: dict[str, Any]
    ) -> list[ContentBlock]:
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            raise McpError(
                ErrorData(code=INVALID_PARAMS, message="`prompt` is required")
            )
        deps = AsyncTaskDeps(
            broker=self._broker_client(),
            publisher=self._backplane_publisher(),
        )
        try:
            result = await submit_inference_task(
                prompt=prompt,
                deps=deps,
                model=arguments.get("model"),
                params=arguments.get("params"),
                kind=arguments.get("kind", "codegen"),
                workspace_path=arguments.get("workspace_path"),
            )
        except McpError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise McpError(
                ErrorData(code=INTERNAL_ERROR, message=str(exc))
            ) from exc
        return _json_text(result)

    async def _dispatch(
        self, name: str, arguments: dict[str, Any]
    ) -> list[ContentBlock]:
        from zelosmcp.loader import get_subagent

        if name == ASYNC_TASK_TOOL["name"]:
            return await self._call_submit_task(arguments)
        meta = get_subagent(name)
        if meta is not None:
            return await self._call_subagent(meta, arguments)
        raise McpError(
            ErrorData(code=METHOD_NOT_FOUND, message=f"Unknown tool: {name!r}")
        )

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        if self.running:
            return
        self.error = None
        self._ready = asyncio.Event()
        self._startup_error = None
        self._task = asyncio.create_task(self._run())
        await self._ready.wait()
        if self._startup_error is not None:
            raise self._startup_error

    async def stop(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        if self._broker is not None:
            with suppress(Exception):
                await self._broker.aclose()
            self._broker = None
        if self._publisher is not None:
            with suppress(Exception):
                await self._publisher.close()
            self._publisher = None

    async def _run(self) -> None:
        """Lifecycle task — owns the streamable-HTTP transport for /zelos/mcp.

        Unlike :class:`zelosmcp.builtin.BuiltinServer` this server runs only
        the HTTP transport (no in-memory aggregator pair) because it is
        intentionally excluded from the aggregator.
        """
        self._emit_log("Starting data-path MCP...")
        srv = Server(self.name)
        self._register_handlers(srv)
        try:
            async with AsyncExitStack() as stack:
                self.session_manager = StreamableHTTPSessionManager(
                    app=srv,
                    event_store=None,
                    json_response=True,
                    stateless=True,
                )
                await stack.enter_async_context(self.session_manager.run())
                self.running = True
                self._emit_log("Data-path MCP live (/zelos/mcp)")
                self._ready.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self._emit_log("Stopping data-path MCP...")
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            self._emit_log(f"ERROR: {exc}")
            self._startup_error = exc
            self._ready.set()
        finally:
            self.session_manager = None
            self.running = False
            self._emit_log("Data-path MCP stopped")

    def _register_handlers(self, srv: Server) -> None:
        @srv.list_tools()
        async def list_tools() -> list[Tool]:
            return list(self._tools)

        @srv.call_tool(validate_input=False)
        async def call_tool(
            name: str, arguments: dict[str, Any]
        ) -> list[ContentBlock]:
            self._emit_log(f"call_tool: {name}")
            return await self._dispatch(name, arguments or {})
