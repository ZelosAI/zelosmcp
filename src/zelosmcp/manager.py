"""Coordinates many ProxyState instances behind one HTTP surface.

The manager is what `app.py` actually talks to. It owns a per-name registry of
:class:`zelosmcp.proxy.ProxyState` objects, tracks which one is the primary
(mirrored at ``/mcp`` instead of just ``/<name>/mcp``), and aggregates log
subscriptions across all servers.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx
from starlette.responses import JSONResponse

from zelosmcp.aggregator import Aggregator
from zelosmcp.auth import (
    AuthRegistry,
    AuthStore,
    ProviderTypeUnavailable,
    build_provider,
)
from zelosmcp.builtin import NAME as BUILTIN_NAME, BuiltinServer
from zelosmcp.server import NAME as DATAPATH_NAME, DataPathServer
from zelosmcp.config import (
    AuthProviderSpec,
    ConfigError,
    ServerSpec,
    parse_auth_providers,
    parse_config,
    validate_provider_references,
)
from zelosmcp.proxy import ProxyState
from zelosmcp.savings import EventRecorder, SavingsRecorder, TokenCounter
from zelosmcp.savings_db import SavingsStore, resolve_db_path


# Default lookup order for the mandatory MCP set. First existing path wins.
# - /app/configs is the root Dockerfile's runtime path.
# - /opt/zelosmcp/configs is the cert-aware Dockerfile's runtime path.
# - The repo-relative path lets developers run uvicorn directly from the
#   working tree (tests can use ProxyManager(mandatory_config_path="") to
#   skip mandatory entirely).
_MANDATORY_PATH_CANDIDATES: tuple[str, ...] = (
    "/app/configs/mandatory-zelosmcp.json",
    "/opt/zelosmcp/configs/mandatory-zelosmcp.json",
    str(Path(__file__).resolve().parent.parent.parent / "configs" / "mandatory-zelosmcp.json"),
)


# Hop-by-hop headers per RFC 7230 §6.1 plus a few headers httpx manages
# itself (Host gets rewritten to the upstream's authority; Content-Length
# is recomputed from the forwarded body). Stripped on both request and
# response sides of the proxy.
_HOP_BY_HOP: frozenset[str] = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
})

# Response headers that prevent the zelosMCP UI from embedding a
# reverse-proxied dashboard in an iframe. Backends like pincher correctly set
# these for direct exposure, but zelosMCP's reverseProxy feature is explicitly a
# trusted same-origin embedding surface (e.g. /pincher/v1/dashboard in the
# dashboard view). Strip them at the proxy boundary so the browser doesn't
# block the iframe with a blank/broken frame.
_FRAME_DENY_HEADERS: frozenset[str] = frozenset({
    "x-frame-options",
    "content-security-policy",
})

_EVENT_RETENTION_HOURS_DEFAULT = 168
_EVENT_RETENTION_HOURS_MIN = 1
_EVENT_RETENTION_HOURS_MAX = 8760
_EVENT_PRUNE_INTERVAL_MINS_DEFAULT = 30
_EVENT_PRUNE_INTERVAL_MINS_MIN = 1
_EVENT_PRUNE_INTERVAL_MINS_MAX = 1440


def _read_bounded_env_int(
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


_log = logging.getLogger("zelosmcp.manager")


class ProxyManager:
    """Lifecycle owner for many ProxyStates plus the /mcp aggregator."""

    def __init__(self, mandatory_config_path: str | None = None) -> None:
        # ``self.servers`` mixes user-configured ProxyStates with the
        # always-on BuiltinServer (under the reserved key "zelosmcp").
        # The builtin is ProxyState-shaped, so the dispatcher and
        # aggregator iterate over it transparently. Lifecycle methods
        # (start_all/stop_all/start_one/stop_one) explicitly skip it.
        self.servers: dict[str, Any] = {}
        self._specs: dict[str, ServerSpec] = {}
        self._builtin_config: "BuiltinConfig | None" = None
        self._primary: str | None = None
        self._log_subscribers: list[asyncio.Queue[str]] = []
        # Ring buffer of every line ever broadcast, capped to keep memory
        # bounded. New SSE subscribers replay this snapshot before
        # entering live-tail so the activity panel reflects the full
        # session history (including startup banners that fired before
        # the browser connected).
        self._log_history: collections.deque[str] = collections.deque(maxlen=2000)
        self._log_pumps: dict[str, asyncio.Task] = {}
        self.aggregator = Aggregator(self)
        # The BuiltinServer is ProxyState-shaped and pre-seeded so the
        # dispatcher can route /zelosmcp/mcp from the very first request.
        # Its log pump is attached lazily from `start_builtin()` because
        # `_attach_log_pump` requires a running event loop.
        self.builtin = BuiltinServer(self)
        self.servers[BUILTIN_NAME] = self.builtin
        # The data-path MCP server (v0.3: sync subagents + async task) is
        # ProxyState-shaped and pre-seeded under the reserved key "zelos" so
        # the dispatcher can route /zelos/mcp. It deliberately exposes no
        # client_session, so the /mcp aggregator and /api/catalog skip it —
        # its tools are invoked directly at /zelos/mcp. Started/stopped via
        # start_datapath()/stop_datapath() from the lifespan hook.
        self.datapath = DataPathServer(self)
        self.servers[DATAPATH_NAME] = self.datapath
        # Shared HTTP client used by the reverse-proxy dispatcher to forward
        # requests to backend HTTP sidecars. Lifecycle is owned by app.py's
        # lifespan hook (start_http_client / stop_http_client) so connection
        # pooling survives across config reloads. Tests can replace the
        # client by setting this attribute directly.
        self._http_client: httpx.AsyncClient | None = None
        # Mandatory MCP set merged into every start_all() payload before
        # parsing. ``None`` (default) auto-discovers from the runtime paths
        # in _MANDATORY_PATH_CANDIDATES; ``""`` disables mandatory merging
        # entirely (used by tests to avoid spawning real subprocesses);
        # any other value is the literal path to the mandatory JSON.
        self._mandatory_config_path = mandatory_config_path
        # Cached parsed mandatory ``mcpServers`` dict (populated lazily on
        # first read by _read_mandatory_servers; reused across start_all()
        # invocations within a single ProxyManager lifetime).
        self._mandatory_cache: dict[str, Any] | None = None
        self._mandatory_payload_cache: dict[str, Any] | None = None
        self._mandatory_cache_loaded = False
        # Token-savings store + recorder. The store is opened from the
        # Starlette lifespan hook (start_http_client) so an event loop is
        # already running; tests that don't go through the lifespan hook
        # can call ``ProxyManager.start_savings(":memory:")`` directly,
        # or attach their own SavingsRecorder via ``manager.savings``.
        self._savings_store: SavingsStore | None = None
        self.savings: SavingsRecorder | None = None
        self.events: EventRecorder | None = None
        self._pincher_poll_task: asyncio.Task | None = None
        self._event_prune_task: asyncio.Task | None = None
        # How often to snapshot ``pincher__stats`` into the savings store.
        # Configurable via ``ZELOSMCP_PINCHER_POLL_SECS``; <=0 disables.
        try:
            self._pincher_poll_interval: float = float(
                os.environ.get("ZELOSMCP_PINCHER_POLL_SECS", "60")
            )
        except ValueError:
            self._pincher_poll_interval = 60.0
        self._event_retention_hours = _read_bounded_env_int(
            "ZELOSMCP_EVENT_RETENTION_HOURS",
            default=_EVENT_RETENTION_HOURS_DEFAULT,
            minimum=_EVENT_RETENTION_HOURS_MIN,
            maximum=_EVENT_RETENTION_HOURS_MAX,
        )
        self._event_prune_interval_mins = _read_bounded_env_int(
            "ZELOSMCP_EVENT_PRUNE_INTERVAL_MINS",
            default=_EVENT_PRUNE_INTERVAL_MINS_DEFAULT,
            minimum=_EVENT_PRUNE_INTERVAL_MINS_MIN,
            maximum=_EVENT_PRUNE_INTERVAL_MINS_MAX,
        )
        # Auth-provider plumbing. Registry holds constructed providers;
        # the spec dict is the source-of-truth for the GET config endpoint
        # (so the UI sees what was loaded, not the constructed provider).
        # Both populated by start_auth_providers (called from app.py
        # lifespan or POST /api/auth/providers/config). The store opens
        # alongside the savings store.
        self.auth_registry: AuthRegistry = AuthRegistry()
        self._auth_provider_specs: dict[str, AuthProviderSpec] = {}
        self._auth_store: AuthStore | None = None
        # Asset store — rules, extensions, agents, hooks.  Opened from
        # start_http_client() alongside the savings store; seeded from
        # configs/assets/ on first open.  None when unavailable (e.g.
        # tests that skip the lifespan hook) — callers degrade gracefully.
        self._assets_store: Any | None = None
        self.assets: Any | None = None

    @property
    def primary(self) -> str | None:
        return self._primary

    @property
    def event_retention_hours(self) -> int:
        return self._event_retention_hours

    @property
    def event_prune_interval_mins(self) -> int:
        return self._event_prune_interval_mins

    def primary_state(self) -> ProxyState | None:
        if self._primary is None:
            return None
        return self.servers.get(self._primary)

    def get(self, name: str) -> ProxyState | None:
        return self.servers.get(name)

    def get_spec(self, name: str) -> ServerSpec | None:
        """Public accessor for a backend's parsed :class:`ServerSpec`.

        Returns ``None`` for unknown names or for the always-on builtin
        (which has no user-supplied spec). Used by the ASGI dispatcher to
        dispatch passthrough backends through ``proxy_mcp_request`` —
        passthrough mode requires the spec's ``url`` and ``auth_bearer``.
        """
        return self._specs.get(name)

    def names(self) -> list[str]:
        return list(self.servers.keys())

    async def start_all(self, raw_config: Any) -> dict[str, Any]:
        """Replace the current set of servers with whatever ``raw_config`` defines.

        Stops anything currently running, parses the config (after merging
        the mandatory MCP set on top of the user payload — user wins on
        same-name collisions), concurrently starts each backend, then brings
        up the aggregator at ``/mcp``. Returns a per-server result map.
        """
        await self.stop_all()

        merged = self._merge_mandatory(raw_config)
        specs, primary, builtin_cfg = parse_config(merged)
        self._apply_event_settings(merged)
        if self._savings_store is not None:
            await self._restart_event_prune_task()
        # Cross-validate any auth.provider references against the
        # currently-loaded providers config. Empty providers dict +
        # zero references = no-op; non-empty references against an
        # empty / mismatched set raises here so a misconfigured config
        # can't silently start backends in a broken state.
        validate_provider_references(specs, self._auth_provider_specs)
        self._specs = {s.name: s for s in specs}
        self._builtin_config = builtin_cfg

        if primary is not None:
            self._broadcast_tagged(
                "aggregator",
                "primaryMCP is deprecated and ignored — "
                "/mcp now aggregates all servers",
            )
        self._primary = None

        results: dict[str, Any] = {}
        coros = []
        started_specs: list[ServerSpec] = []
        for spec in specs:
            if spec.name == BUILTIN_NAME:
                # parse_config already rejects this via RESERVED_NAMES, but
                # belt-and-braces in case a future code path bypasses it.
                continue
            state = ProxyState(name=spec.name)
            state.set_recorders(
                recorder_provider=lambda: self.savings,
                event_recorder_provider=lambda: self.events,
            )
            self.servers[spec.name] = state
            self._attach_log_pump(state)
            if not spec.started:
                self._broadcast_tagged(
                    spec.name,
                    "configured but not started "
                    "(started: false)",
                )
                results[spec.name] = {
                    "ok": True, "started": False,
                }
                continue
            started_specs.append(spec)
            coros.append(self._start_one_spec(state, spec))

        outcomes = await asyncio.gather(*coros, return_exceptions=True)
        for spec, outcome in zip(started_specs, outcomes):
            if isinstance(outcome, BaseException):
                results[spec.name] = {"ok": False, "error": str(outcome)}
            else:
                results[spec.name] = {"ok": True}

        if any(s.running for s in self.servers.values()):
            try:
                await self.aggregator.start()
            except Exception as exc:
                self._broadcast_tagged("aggregator", f"failed to start: {exc}")

        # Auto-generate default rule assets for any running backend that has
        # no rows in the asset store (backends without a <name>.yaml file).
        if self.assets is not None:
            try:
                from zelosmcp.framework.assetstore.defaults import ensure_default_assets
                for name, state in list(self.servers.items()):
                    if not getattr(state, "running", False):
                        continue
                    n = await ensure_default_assets(self.assets, self, name)
                    if n:
                        self._broadcast_tagged(
                            "assets",
                            f"auto-generated {n} default rule row(s) for '{name}'",
                        )
            except Exception as exc:
                _log.warning("ensure_default_assets sweep failed: %s", exc)

        return {
            "primary": None,
            "servers": results,
        }

    def _read_event_setting(
        self,
        raw_config: Any,
        *,
        key: str,
        env_name: str,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        if isinstance(raw_config, dict) and key in raw_config:
            value = raw_config[key]
            if not isinstance(value, int) or isinstance(value, bool):
                raise ConfigError(
                    f"'{key}' must be an integer between {minimum} and {maximum}"
                )
            if value < minimum or value > maximum:
                raise ConfigError(
                    f"'{key}' must be between {minimum} and {maximum}"
                )
            return value
        return _read_bounded_env_int(
            env_name,
            default=default,
            minimum=minimum,
            maximum=maximum,
        )

    def _apply_event_settings(self, raw_config: Any) -> None:
        self._event_retention_hours = self._read_event_setting(
            raw_config,
            key="event_retention_hours",
            env_name="ZELOSMCP_EVENT_RETENTION_HOURS",
            default=_EVENT_RETENTION_HOURS_DEFAULT,
            minimum=_EVENT_RETENTION_HOURS_MIN,
            maximum=_EVENT_RETENTION_HOURS_MAX,
        )
        self._event_prune_interval_mins = self._read_event_setting(
            raw_config,
            key="event_prune_interval_mins",
            env_name="ZELOSMCP_EVENT_PRUNE_INTERVAL_MINS",
            default=_EVENT_PRUNE_INTERVAL_MINS_DEFAULT,
            minimum=_EVENT_PRUNE_INTERVAL_MINS_MIN,
            maximum=_EVENT_PRUNE_INTERVAL_MINS_MAX,
        )

    async def _restart_event_prune_task(self) -> None:
        task, self._event_prune_task = self._event_prune_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        if self._savings_store is None or self.events is None:
            return
        self._event_prune_task = asyncio.create_task(self._event_prune_loop())

    async def _prune_events_once(self) -> int:
        store = self._savings_store
        if store is None:
            return 0
        cutoff_ts = time.time() - (self._event_retention_hours * 3600)
        try:
            deleted = await store.prune_before(cutoff_ts)
        except Exception as exc:
            _log.warning("events prune failed: %s", exc)
            return 0
        if deleted > 0:
            self._broadcast_tagged(
                "events",
                f"Pruned {deleted} events older than {self._event_retention_hours}h",
            )
        return deleted

    async def _event_prune_loop(self) -> None:
        interval_secs = self._event_prune_interval_mins * 60
        try:
            while True:
                await asyncio.sleep(interval_secs)
                await self._prune_events_once()
        except asyncio.CancelledError:
            pass

    def _merge_mandatory(self, raw_config: Any) -> Any:
        """Merge the mandatory MCP set into ``raw_config``.

        Returns a new dict (the input is left unmodified). Mandatory
        backends fill in any names absent from the user's payload; if the
        user's payload defines an entry with the same name, the user's
        entry wins so they can override args/env/etc.

        Returns the input unchanged when:
        - ``mandatory_config_path`` is set to the empty string (test mode).
        - The mandatory file doesn't exist or fails to parse.
        - ``raw_config`` isn't a dict (parse_config will raise downstream).
        """
        mandatory_payload = self._read_mandatory_payload()
        mandatory = (
            mandatory_payload.get("mcpServers")
            if mandatory_payload else None
        )
        if not mandatory or not isinstance(raw_config, dict):
            return raw_config

        merged = dict(raw_config)
        user_servers = merged.get("mcpServers")
        user_servers = dict(user_servers) if isinstance(user_servers, dict) else {}

        injected: list[str] = []
        for name, entry in mandatory.items():
            if name not in user_servers:
                user_servers[name] = entry
                injected.append(name)
            else:
                # Field-level merge: fill in top-level fields present in the
                # mandatory entry that the user's entry omits. User fields
                # always win on conflict (right-hand spread), so the caller can
                # still override args/env/compress/etc. This prevents
                # infrastructure fields like `reverseProxy` from being silently
                # dropped when the caller only supplies a partial override
                # (e.g. changing only compression level).
                user_servers[name] = {**entry, **user_servers[name]}

        merged["mcpServers"] = user_servers

        # Merge the mandatory ``builtin`` block when the user
        # config doesn't provide one.
        if "builtin" not in merged and mandatory_payload:
            mand_builtin = mandatory_payload.get("builtin")
            if mand_builtin is not None:
                merged["builtin"] = mand_builtin

        if injected:
            self._broadcast_tagged(
                "manager",
                f"merged mandatory backends: {', '.join(sorted(injected))}",
            )
        return merged

    def _read_mandatory_payload(self) -> dict[str, Any] | None:
        """Read and cache the full mandatory config payload.

        Returns ``None`` when mandatory is disabled, the file is
        missing, or the file can't be parsed. Subsequent calls return
        the cached result without re-reading the file.
        """
        if self._mandatory_cache_loaded:
            return self._mandatory_payload_cache

        self._mandatory_cache_loaded = True
        path = self._resolve_mandatory_path()
        if path is None:
            return None

        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except FileNotFoundError:
            _log.info("mandatory config not found at %s; skipping merge", path)
            return None
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning("mandatory config %s failed to load: %s", path, exc)
            return None

        if not isinstance(payload, dict):
            _log.warning(
                "mandatory config %s is not a JSON object; skipping",
                path,
            )
            return None

        servers = payload.get("mcpServers")
        if not isinstance(servers, dict):
            _log.warning(
                "mandatory config %s missing 'mcpServers' object; skipping merge",
                path,
            )
            return None

        self._mandatory_payload_cache = payload
        # Keep legacy cache for mandatory_names() compat
        self._mandatory_cache = servers
        return payload

    def _read_mandatory_servers(
        self,
    ) -> dict[str, Any] | None:
        """Return the ``mcpServers`` dict from the mandatory file."""
        payload = self._read_mandatory_payload()
        if payload is None:
            return None
        return payload.get("mcpServers")

    def mandatory_names(self) -> set[str]:
        """Set of backend names declared in the mandatory config.

        Used by the cursor-rule generator to decide which backends get
        a curated playbook block. Returns an empty set when the
        mandatory config is disabled, missing, or unreadable so callers
        can treat "no mandatory backends" and "no playbook" identically.
        """
        servers = self._read_mandatory_servers() or {}
        return set(servers.keys())

    def _resolve_mandatory_path(self) -> str | None:
        """Pick the actual mandatory-file path to read.

        Honours the constructor's ``mandatory_config_path`` argument:
        - ``""`` => disabled (test mode).
        - non-empty string => use that path verbatim.
        - ``None`` (default) => first existing path in
          _MANDATORY_PATH_CANDIDATES, or ``None`` if none exist.
        """
        explicit = self._mandatory_config_path
        if explicit == "":
            return None
        if isinstance(explicit, str) and explicit:
            return explicit
        for candidate in _MANDATORY_PATH_CANDIDATES:
            if os.path.isfile(candidate):
                return candidate
        return None

    async def _stop_user_backends_only(self) -> None:
        """Stop every user-configured backend but leave the aggregator's
        session_manager running. This is the hot-swap path used by
        :meth:`start_all` so inbound ``/mcp`` requests never see a 503
        window while backends are being recycled.

        The always-on builtin (`zelosmcp`) and data-path (`zelos`) servers
        are preserved unchanged.
        """
        to_stop = [
            (name, state)
            for name, state in self.servers.items()
            if name not in (BUILTIN_NAME, DATAPATH_NAME)
        ]
        if to_stop:
            await asyncio.gather(
                *(state.stop() for _, state in to_stop),
                return_exceptions=True,
            )
        for name, _ in to_stop:
            task = self._log_pumps.pop(name, None)
            if task is not None:
                task.cancel()
        for name, _ in to_stop:
            self.servers.pop(name, None)

    async def stop_all(self) -> None:
        """Stop every user-configured backend AND the aggregator. The
        always-on builtin (`zelosmcp`) is intentionally preserved so its
        tools remain available across config reloads.

        This is the hard stop used by ``/api/stop``. Config reloads via
        ``/api/start`` use :meth:`_stop_user_backends_only` instead so
        the aggregator's ``session_manager`` survives the swap and
        Cursor's MCP SSE connection doesn't get a 503.
        """
        await self.aggregator.stop()
        await self._stop_user_backends_only()
        self._specs.clear()
        self._primary = None

    async def start_one(self, name: str) -> None:
        if name in (BUILTIN_NAME, DATAPATH_NAME):
            raise KeyError(
                f"'{name}' is an always-on built-in backend and cannot be "
                "started/stopped"
            )
        spec = self._specs.get(name)
        if spec is None:
            raise KeyError(f"No server named '{name}'")
        state = self.servers.get(name)
        if state is None:
            state = ProxyState(name=name)
            state.set_recorders(
                recorder_provider=lambda: self.savings,
                event_recorder_provider=lambda: self.events,
            )
            self.servers[name] = state
            self._attach_log_pump(state)
        if state.running:
            raise RuntimeError(f"Server '{name}' is already running")
        await self._start_one_spec(state, spec)

    async def stop_one(self, name: str) -> None:
        if name in (BUILTIN_NAME, DATAPATH_NAME):
            raise KeyError(
                f"'{name}' is an always-on built-in backend and cannot be "
                "started/stopped"
            )
        state = self.servers.get(name)
        if state is None:
            raise KeyError(f"No server named '{name}'")
        await state.stop()

    async def start_builtin(self) -> None:
        """Bring up the always-on builtin MCP. Called once from the
        Starlette lifespan hook in :func:`zelosmcp.app.create_app` before
        any HTTP request arrives. Idempotent."""
        if self.builtin.running:
            return
        await self.builtin.start()
        # Now that we have a running event loop, hook the builtin's log
        # stream into the manager's broadcast set so its activity shows
        # up in `/api/logs` like every other backend.
        if BUILTIN_NAME not in self._log_pumps:
            self._attach_log_pump(self.builtin)
        # The aggregator depends on at least one running backend;
        # making sure it's live as soon as the builtin is up means
        # /mcp can serve `zelosmcp__*` tools even when no user
        # backend has been configured yet.
        if not self.aggregator.running:
            try:
                await self.aggregator.start()
            except Exception as exc:
                self._broadcast_tagged("aggregator", f"failed to start: {exc}")

    async def stop_builtin(self) -> None:
        """Tear down the builtin. Called from the lifespan shutdown hook."""
        await self.aggregator.stop()
        task = self._log_pumps.pop(BUILTIN_NAME, None)
        if task is not None:
            task.cancel()
        await self.builtin.stop()

    async def start_datapath(self) -> None:
        """Bring up the always-on data-path MCP server (/zelos/mcp).

        Called once from the Starlette lifespan startup hook. Idempotent. The
        data-path server is intentionally NOT wired into the aggregator (it
        has no client_session), so unlike start_builtin this does not touch
        the aggregator.
        """
        if self.datapath.running:
            return
        await self.datapath.start()
        if DATAPATH_NAME not in self._log_pumps:
            self._attach_log_pump(self.datapath)

    async def stop_datapath(self) -> None:
        """Tear down the data-path server. Called from the lifespan shutdown."""
        task = self._log_pumps.pop(DATAPATH_NAME, None)
        if task is not None:
            task.cancel()
        await self.datapath.stop()

    async def start_http_client(self) -> None:
        """Initialise the shared httpx.AsyncClient used by the reverse-proxy
        dispatcher. Called once from the Starlette lifespan startup hook.
        Idempotent."""
        if self._http_client is not None:
            return
        # Modest connect timeout so a stopped backend fails fast; longer
        # read window so streaming responses (dashboard HTML, slow query
        # endpoints) don't get cut off. follow_redirects stays off — we
        # forward the upstream's redirect response verbatim instead.
        #
        # Trust the system CA bundle (Debian path) when present so any
        # corporate root certs installed via `update-ca-certificates`
        # at image-build time are honoured. httpx's default uses certifi
        # alone, which doesn't pick up corporate roots — that breaks
        # outbound calls through TLS-intercepting proxies (Zscaler,
        # corporate egress gateways, etc.). Falls back to the default
        # bundled bundle when the system file is absent (e.g. running
        # outside the container during tests).
        system_ca = "/etc/ssl/certs/ca-certificates.crt"
        verify: Any = system_ca if os.path.exists(system_ca) else True
        self._http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0),
            follow_redirects=False,
            verify=verify,
        )
        await self.start_savings()
        await self.start_assets()

    async def stop_http_client(self) -> None:
        """Close the shared httpx.AsyncClient. Called from the lifespan
        shutdown hook. Idempotent."""
        client, self._http_client = self._http_client, None
        if client is not None:
            try:
                await client.aclose()
            except Exception as exc:
                _log.warning("reverse-proxy: client close failed: %s", exc)
        await self.stop_assets()
        await self.stop_savings()

    async def start_savings(self, db_path: str | None = None) -> None:
        """Open the savings store and start the pincher snapshot poller.

        Idempotent; tests may pass ``db_path=":memory:"`` (or set the
        ``ZELOSMCP_SAVINGS_DB`` env var) to skip on-disk state.
        """
        if self.savings is not None:
            return
        path = resolve_db_path(db_path)
        store = SavingsStore(path)
        try:
            await store.open()
        except Exception as exc:
            _log.warning("savings store open failed (%s); disabling", exc)
            self._broadcast_tagged("savings", f"disabled: {exc}")
            return
        counter = TokenCounter()
        # Warmup is best-effort — heuristic fallback handles failures.
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, counter.warmup
            )
        except Exception:
            pass
        self._savings_store = store
        self.savings = SavingsRecorder(store=store, counter=counter)
        self.events = EventRecorder(store=store, counter=counter)
        await self._restart_event_prune_task()
        if counter.using_heuristic:
            self._broadcast_tagged(
                "savings",
                "tiktoken unavailable; using char/4 heuristic",
            )
        else:
            self._broadcast_tagged("savings", "token counter ready (cl100k_base)")
        if self._pincher_poll_interval > 0:
            self._pincher_poll_task = asyncio.create_task(self._pincher_poll_loop())

    async def stop_savings(self) -> None:
        task, self._pincher_poll_task = self._pincher_poll_task, None
        prune_task, self._event_prune_task = self._event_prune_task, None
        for active_task in (task, prune_task):
            if active_task is None:
                continue
            active_task.cancel()
            try:
                await active_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        store, self._savings_store = self._savings_store, None
        self.savings = None
        self.events = None
        if store is not None:
            await store.close()

    async def start_assets(self, db_path: str | None = None) -> None:
        """Open the asset store and seed it from the bundled YAML files.

        Idempotent.  Failures are logged at WARNING and the store stays
        ``None`` — callers degrade gracefully (rule renderer falls back to
        hardcoded defaults, extension invoke returns 503, push returns 503).
        """
        if self._assets_store is not None:
            return
        try:
            from zelosmcp.framework.assetstore import (
                SQLiteAssetStore,
                resolve_db_path as _resolve_assets_db,
                seed_all,
            )
        except ImportError as exc:
            _log.warning("assets: framework not available (%s); skipping", exc)
            return

        path = _resolve_assets_db(db_path)
        store = SQLiteAssetStore(path)
        try:
            await store.open()
        except Exception as exc:
            _log.warning("assets store open failed (%s); disabling", exc)
            self._broadcast_tagged("assets", f"disabled: {exc}")
            return

        self._assets_store = store
        self.assets = store

        try:
            counts = await seed_all(store)
            total = sum(counts.values())
            if total:
                self._broadcast_tagged(
                    "assets",
                    f"seeded {total} rows: "
                    + ", ".join(f"{k}={n}" for k, n in sorted(counts.items()) if n),
                )
            else:
                self._broadcast_tagged("assets", "store ready (no seed rows)")
        except Exception as exc:
            _log.warning("assets seeder failed: %s", exc)
            self._broadcast_tagged("assets", f"seed failed: {exc}")

    async def stop_assets(self) -> None:
        """Close the asset store.  Idempotent."""
        store, self._assets_store = self._assets_store, None
        self.assets = None
        if store is not None:
            try:
                await store.close()
            except Exception as exc:
                _log.warning("assets store close failed: %s", exc)

    async def start_auth_store(
        self, db_path: str | None = None, key_path: Any | None = None
    ) -> None:
        """Open the encrypted auth store. Idempotent.

        Parallel to :meth:`start_savings`. The store is needed by every
        provider that maintains per-user state (i.e. all real OAuth
        providers); ``passthrough`` and ``static`` providers don't
        touch it. Tests can pass ``db_path=":memory:"`` to skip
        on-disk state.
        """
        if self._auth_store is not None:
            return
        try:
            store = AuthStore.open_with_key_file(
                path=db_path, key_path=key_path,
            )
            await store.open()
        except Exception as exc:
            # Don't crash the whole app — auth store failures only
            # break OAuth providers; passthrough / static still work
            # without it. Surface in the activity log so the GUI can
            # tell the user.
            _log.warning("auth store open failed (%s); disabling", exc)
            self._broadcast_tagged("auth", f"store disabled: {exc}")
            return
        self._auth_store = store
        self._broadcast_tagged("auth", "store ready")

    async def stop_auth_store(self) -> None:
        store, self._auth_store = self._auth_store, None
        if store is not None:
            await store.close()

    @property
    def auth_store(self) -> AuthStore | None:
        """Public accessor used by provider factories that need
        per-user persistence. ``None`` when the store hasn't been
        opened (test mode without ``start_auth_store``)."""
        return self._auth_store

    async def start_auth_providers(self, raw_config: Any) -> dict[str, Any]:
        """Parse and load the auth-providers config.

        Replaces the entire registry atomically. Spec parsing happens
        first; if any spec is invalid the registry is left untouched
        so a bad POST doesn't drop the working set. Provider
        instantiation happens after parse — types whose factory isn't
        registered yet (because their PR hasn't landed) are tracked
        separately and surface in the result map.

        Returns ``{"providers": {<name>: <status>, ...}}`` where
        status is ``"ready"`` for fully-constructed providers,
        ``"unavailable"`` when no factory is registered, or
        ``"error: <msg>"`` when construction failed.
        """
        specs = parse_auth_providers(raw_config)

        results: dict[str, str] = {}
        constructed: list[Any] = []
        for name, spec in specs.items():
            try:
                provider = build_provider(spec, self._auth_store)
            except ProviderTypeUnavailable:
                results[name] = "unavailable"
                continue
            except Exception as exc:  # noqa: BLE001 — surface to caller
                results[name] = f"error: {exc}"
                continue
            constructed.append(provider)
            results[name] = "ready"

        try:
            self.auth_registry.replace_all(constructed)
        except ValueError as exc:
            # Should be unreachable since parse_auth_providers already
            # rejects duplicates; defensive in case the factory ever
            # returns a renamed instance.
            raise ValueError(
                f"auth provider registration failed: {exc}"
            ) from exc

        self._auth_provider_specs = specs
        # Re-validate currently-loaded server references against the
        # new provider set so a swap that drops a referenced provider
        # surfaces immediately rather than at the next /api/start.
        validate_provider_references(
            list(self._specs.values()), specs,
        )
        if specs:
            self._broadcast_tagged(
                "auth",
                f"loaded {len(specs)} provider(s): {', '.join(sorted(specs))}",
            )
        return {"providers": results}

    async def regenerate_assets_for_provider(
        self, provider_name: str
    ) -> dict[str, int]:
        """Re-run default-asset generation for backends wired to ``provider_name``.

        Called from the HTTP auth routes when a provider's per-user state
        transitions (OAuth callback completes, device flow finishes, revoke).
        The backend's live tool list typically goes from 0 → N (connect) or
        N → 0 (revoke) at those moments — the stored auto-default playbook
        must reflect that transition.

        Updates only auto-generated default rows (``source='seed'``,
        ``seed_version=0``); user edits and YAML-seeded rows
        (``seed_version >= 1``) are preserved.

        Returns ``{backend_name: rows_written}`` for every backend whose
        defaults changed.  Empty when the asset store isn't open, no
        backend references the provider, or no rows changed.
        """
        if self.assets is None:
            return {}

        try:
            from zelosmcp.framework.assetstore.defaults import (
                regenerate_default_assets,
            )
        except Exception as exc:  # noqa: BLE001 — degrade gracefully
            _log.warning(
                "regenerate_assets_for_provider: import failed: %s", exc
            )
            return {}

        results: dict[str, int] = {}
        for backend_name, spec in list(self._specs.items()):
            if spec.auth_provider != provider_name:
                continue
            state = self.servers.get(backend_name)
            if state is None or not getattr(state, "running", False):
                continue
            try:
                n = await regenerate_default_assets(
                    self.assets, self, backend_name
                )
            except Exception as exc:
                _log.warning(
                    "regenerate_assets_for_provider: %s failed: %s",
                    backend_name, exc,
                )
                continue
            if n:
                results[backend_name] = n
                self._broadcast_tagged(
                    "assets",
                    f"regenerated {n} default rule row(s) for "
                    f"'{backend_name}' after auth provider "
                    f"'{provider_name}' state change",
                )
        return results

    def current_auth_providers_config(
        self, *, redacted: bool = True
    ) -> dict[str, Any]:
        """JSON snapshot of the loaded providers config — what the GUI
        renders in the Connections page and what
        ``GET /api/auth/providers/config`` returns.

        ``redacted=True`` (the default) replaces secret-like fields
        with ``"***"``; ``False`` returns the raw values for trusted
        callers (currently nobody, but useful in tests).
        """
        return {
            "providers": {
                name: spec.to_status(redacted=redacted)
                for name, spec in self._auth_provider_specs.items()
            }
        }

    async def _pincher_poll_loop(self) -> None:
        """Periodically snapshot ``pincher__stats`` into the savings store.

        Skips silently while pincher isn't running. Resilient: any failure
        is logged at debug and the loop sleeps the full interval before
        retrying so one upstream blip doesn't spam writes.
        """
        interval = self._pincher_poll_interval
        try:
            while True:
                await asyncio.sleep(interval)
                if self.savings is None:
                    continue
                state = self.servers.get("pincher")
                session = getattr(state, "client_session", None)
                if state is None or not getattr(state, "running", False) or session is None:
                    continue
                try:
                    result = await session.call_tool("stats", {})
                except Exception as exc:
                    _log.debug("pincher stats poll failed: %s", exc)
                    continue
                payload: Any = {
                    "structuredContent": getattr(result, "structuredContent", None),
                    "content": [
                        getattr(c, "text", None) or getattr(c, "type", None)
                        for c in (getattr(result, "content", None) or [])
                    ],
                    "isError": bool(getattr(result, "isError", False)),
                }
                try:
                    await self.savings.record_pincher_stats(payload)
                except Exception as exc:
                    _log.debug("pincher stats snapshot record failed: %s", exc)
        except asyncio.CancelledError:
            pass

    def find_reverse_proxy(
        self, path: str
    ) -> tuple[ServerSpec, Any] | None:
        """Locate the backend whose reverseProxy.mount best matches ``path``.

        Returns ``(spec, state)`` where ``state`` may be ``None`` if the
        backend is configured but not currently running. Returns ``None``
        when no mount matches. Longest-prefix wins so nested mounts like
        ``/foo`` and ``/foo/bar`` would both resolve correctly (though
        ``parse_config`` rejects overlap today, the longest-match rule
        keeps this future-proof).
        """
        best: tuple[ServerSpec, Any] | None = None
        best_len = -1
        for name, spec in self._specs.items():
            if spec.reverse_proxy is None:
                continue
            mount = spec.reverse_proxy.mount
            # Segment-aware: '/foo' must match '/foo' or '/foo/...' but
            # never '/foobar'. Append '/' before testing.
            if path == mount or path.startswith(mount + "/"):
                if len(mount) > best_len:
                    best_len = len(mount)
                    best = (spec, self.servers.get(name))
        return best

    def reverse_proxy_openapi_specs(self) -> list[tuple[ServerSpec, Any]]:
        """Return configured reverse proxies that advertise OpenAPI contracts."""
        out: list[tuple[ServerSpec, Any]] = []
        for name, spec in self._specs.items():
            rp = spec.reverse_proxy
            if rp is None or rp.openapi is None:
                continue
            out.append((spec, self.servers.get(name)))
        return out

    async def fetch_reverse_proxy_openapi(
        self,
        spec: ServerSpec,
        *,
        scheme: str = "http",
        host: str = "",
    ) -> dict[str, Any]:
        """Fetch one backend's OpenAPI document through its configured upstream."""
        rp = spec.reverse_proxy
        assert rp is not None, "fetch_reverse_proxy_openapi called without a reverseProxy"
        assert rp.openapi is not None, "fetch_reverse_proxy_openapi called without openapi"

        client = self._http_client
        if client is None:
            raise RuntimeError("reverse-proxy client not initialised")

        upstream_url = httpx.URL(rp.upstream + rp.openapi.path)
        headers: dict[str, str] = {
            "Accept": "application/json",
            "X-Forwarded-Proto": scheme,
            "X-Forwarded-Prefix": rp.mount,
        }
        if host:
            headers["X-Forwarded-Host"] = host
        headers.update(rp.headers)
        if rp.auth_bearer and "authorization" not in {k.lower() for k in headers}:
            headers["Authorization"] = f"Bearer {rp.auth_bearer}"

        response = await client.get(upstream_url, headers=headers)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("upstream OpenAPI response must be a JSON object")
        return payload

    async def proxy_request(
        self,
        spec: ServerSpec,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        """Forward an ASGI HTTP request to ``spec.reverse_proxy.upstream``.

        Reads the request body in full (non-streaming v1 — pincher's payloads
        are tiny and this keeps the implementation simple). Streams the
        response body back to the caller via ``Response.aiter_raw``.
        """
        rp = spec.reverse_proxy
        assert rp is not None, "proxy_request called without a reverseProxy"
        client = self._http_client
        if client is None:
            resp = JSONResponse(
                {"error": "reverse-proxy client not initialised"},
                status_code=503,
            )
            await resp(scope, receive, send)
            return

        # Build the forwarded URL. When stripPrefix is true we drop the mount
        # before forwarding (so '/foo/v1/x' -> '<upstream>/v1/x'); otherwise
        # the path is forwarded verbatim ('/foo/v1/x' -> '<upstream>/foo/v1/x'),
        # which lets upstreams like pincher honour X-Forwarded-Prefix
        # themselves.
        request_path = scope.get("path", "/")
        if rp.strip_prefix and request_path.startswith(rp.mount):
            forwarded_path = request_path[len(rp.mount) :] or "/"
        else:
            forwarded_path = request_path

        raw_query = scope.get("query_string", b"") or b""
        upstream_url = httpx.URL(rp.upstream + forwarded_path)
        if raw_query:
            upstream_url = upstream_url.copy_with(query=raw_query)

        # Read the full request body. Bounded by the client; non-streaming
        # by design (see docstring).
        body_chunks: list[bytes] = []
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"") or b""
            if chunk:
                body_chunks.append(chunk)
            more = message.get("more_body", False)
        body = b"".join(body_chunks)

        # Build forwarded headers: drop hop-by-hop, then layer X-Forwarded-*,
        # user-configured headers, and bearer auth in that order. Each later
        # layer wins so users can override the canonical X-Forwarded-* set
        # if they need to.
        headers: list[tuple[str, str]] = []
        for raw_name, raw_value in scope.get("headers", []):
            hname = raw_name.decode("latin-1")
            if hname.lower() in _HOP_BY_HOP:
                continue
            headers.append((hname, raw_value.decode("latin-1")))

        client_host = ""
        client_info = scope.get("client")
        if isinstance(client_info, (list, tuple)) and client_info:
            client_host = str(client_info[0])

        scheme = scope.get("scheme", "http")
        host_header = ""
        for raw_name, raw_value in scope.get("headers", []):
            if raw_name == b"host":
                host_header = raw_value.decode("latin-1")
                break

        forwarded: dict[str, str] = {
            "X-Forwarded-Proto": scheme,
            "X-Forwarded-Prefix": rp.mount,
        }
        if host_header:
            forwarded["X-Forwarded-Host"] = host_header
        if client_host:
            # Append onto any existing X-Forwarded-For chain so transparent
            # proxies in front of zelosMCP keep their origin information.
            existing_xff = next(
                (v for k, v in headers if k.lower() == "x-forwarded-for"),
                None,
            )
            forwarded["X-Forwarded-For"] = (
                f"{existing_xff}, {client_host}" if existing_xff else client_host
            )
            # Drop the existing X-Forwarded-For so the merged value wins.
            headers = [
                (k, v) for k, v in headers if k.lower() != "x-forwarded-for"
            ]

        # User-configured headers take precedence over the canonical
        # forwarded set so admins can override (e.g. set a fixed
        # X-Forwarded-Host for clients with a known external hostname).
        for k, v in rp.headers.items():
            forwarded[k] = v

        # Drop any forwarded-key duplicates from the original header list,
        # then append the canonical forwarded set.
        forwarded_lower = {k.lower() for k in forwarded}
        headers = [(k, v) for k, v in headers if k.lower() not in forwarded_lower]
        headers.extend(forwarded.items())

        # Inject bearer token only when the caller hasn't supplied their own
        # Authorization header. Lets clients with their own credentials pass
        # through unchanged.
        if rp.auth_bearer:
            has_auth = any(k.lower() == "authorization" for k, _ in headers)
            if not has_auth:
                headers.append(("Authorization", f"Bearer {rp.auth_bearer}"))

        method = scope.get("method", "GET")
        try:
            req = client.build_request(
                method,
                upstream_url,
                headers=headers,
                content=body if body else None,
            )
            # Non-streaming v1: read the full upstream body before relaying.
            # Pincher's payloads (dashboard HTML, JSON tool responses) are
            # small enough that this keeps the implementation simple. If we
            # later proxy SSE / large downloads, switch to client.send(stream=True)
            # and aiter_bytes().
            upstream_resp = await client.send(req)
        except httpx.RequestError as exc:
            resp = JSONResponse(
                {
                    "error": "reverse-proxy upstream unreachable",
                    "backend": spec.name,
                    "upstream": rp.upstream,
                    "detail": str(exc),
                },
                status_code=502,
            )
            await resp(scope, receive, send)
            return

        response_headers: list[tuple[bytes, bytes]] = []
        for raw_name, raw_value in upstream_resp.headers.raw:
            name_lower = raw_name.decode("latin-1").lower()
            if name_lower in _HOP_BY_HOP or name_lower in _FRAME_DENY_HEADERS:
                continue
            # httpx auto-decompresses the upstream's body (gzip / br /
            # deflate) when we read .content, so a forwarded
            # Content-Encoding header would mismatch the bytes we send.
            # The browser would try to decompress already-plain HTML and
            # render an empty page. Drop the encoding header — at the
            # current size of these responses (dashboard ~50KB, tool
            # JSONs much smaller) the lack of wire compression is
            # negligible.
            if name_lower == "content-encoding":
                continue
            response_headers.append((raw_name, raw_value))

        await send({
            "type": "http.response.start",
            "status": upstream_resp.status_code,
            "headers": response_headers,
        })
        await send({
            "type": "http.response.body",
            "body": upstream_resp.content,
            "more_body": False,
        })

    async def proxy_mcp_request(
        self,
        spec: ServerSpec,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        """Streaming MCP-aware passthrough for /<name>/mcp.

        Used for backends with ``passthrough=True`` so the MCP client's
        OAuth dance flows directly to the upstream issuer. Differs from
        :meth:`proxy_request` in three ways:

        1. Forwards to ``spec.url`` (the MCP endpoint) — there's no
           ``reverseProxy.mount/upstream`` pair to consult.
        2. Streams the response body chunk-by-chunk so MCP's
           ``text/event-stream`` flows survive end-to-end.
        3. Injects ``Authorization: Bearer <auth_bearer>`` only when the
           inbound request has no ``Authorization`` header (static
           fallback for headless / CI scenarios).

        ``WWW-Authenticate`` and other response headers are propagated
        verbatim, sans hop-by-hop, so an upstream 401 reaches the client
        intact and triggers its OAuth handler.
        """
        if spec.url is None:
            resp = JSONResponse(
                {"error": f"backend '{spec.name}' has no url for passthrough"},
                status_code=500,
            )
            await resp(scope, receive, send)
            return

        client = self._http_client
        if client is None:
            resp = JSONResponse(
                {"error": "passthrough HTTP client not initialised"},
                status_code=503,
            )
            await resp(scope, receive, send)
            return

        # Read the inbound body fully. MCP requests are JSON-RPC envelopes
        # — small. Streaming the *response* matters far more than the
        # request, since SSE responses can be long-lived.
        body_chunks: list[bytes] = []
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"") or b""
            if chunk:
                body_chunks.append(chunk)
            more = message.get("more_body", False)
        body = b"".join(body_chunks)

        # Build forwarded headers: drop hop-by-hop, then layer Authorization
        # fallback (only when caller didn't supply one). User-configured
        # ``headers`` from ServerSpec are merged in like the static-bearer
        # case — they win over inbound only when the inbound is absent, so
        # a user can't accidentally lose their per-request Authorization
        # by setting a config-level one.
        outbound_headers: list[tuple[str, str]] = []
        seen_authorization = False
        inbound_authorization_value: str | None = None
        seen_accept = False
        for raw_name, raw_value in scope.get("headers", []):
            hname = raw_name.decode("latin-1")
            lower = hname.lower()
            if lower in _HOP_BY_HOP:
                continue
            # Drop the inbound Host so httpx synthesises one matching the
            # upstream URL — otherwise upstream sees `localhost:8000`.
            if lower == "host":
                continue
            if lower == "authorization":
                seen_authorization = True
                inbound_authorization_value = raw_value.decode("latin-1")
            if lower == "accept":
                seen_accept = True
            outbound_headers.append((hname, raw_value.decode("latin-1")))

        # Broker-mode provider: if this backend references an auth provider
        # and that provider can mint a token for the current user, the
        # minted token wins over any inbound Authorization header. This keeps
        # /<name>/mcp aligned with the aggregator path: users authenticate in
        # the zelosMCP GUI, then both raw passthrough and /mcp aggregate calls
        # forward the brokered upstream token.
        if spec.auth_provider:
            provider = self.auth_registry.get_for_backend(
                spec.name, spec.auth_provider
            )
            if provider is not None:
                try:
                    from zelosmcp.passthrough_pool import hash_authorization

                    user_key = hash_authorization(inbound_authorization_value)
                    minted = await provider.mint_token(
                        user_key, spec.auth_audience
                    )
                except Exception as exc:
                    _log.info(
                        "provider %s mint_token for %s failed: %s",
                        spec.auth_provider, spec.name, exc,
                    )
                    minted = None
                if minted is not None:
                    outbound_headers = [
                        (k, v)
                        for k, v in outbound_headers
                        if k.lower() != "authorization"
                    ]
                    outbound_headers.append(("Authorization", minted))
                    seen_authorization = True

        # Static fallback bearer (only when the caller didn't supply one).
        if not seen_authorization and spec.auth_bearer:
            outbound_headers.append(("Authorization", f"Bearer {spec.auth_bearer}"))

        # Static config-level headers from ServerSpec.headers — merged
        # after Authorization handling. We do NOT overwrite caller-set
        # headers (case-insensitive) so per-Cursor values take priority.
        if spec.headers:
            existing_lower = {k.lower() for k, _ in outbound_headers}
            for k, v in spec.headers.items():
                if k.lower() in existing_lower:
                    continue
                outbound_headers.append((k, v))

        # MCP servers commonly require the dual-Accept header to negotiate
        # JSON vs. event-stream. If the caller didn't set Accept, default
        # to the canonical MCP value so a barebones proxy probe (e.g. our
        # own integration tests) works without extra ceremony.
        if not seen_accept:
            outbound_headers.append(
                ("Accept", "application/json, text/event-stream")
            )

        method = scope.get("method", "POST")
        upstream_url = spec.url

        # Stream the upstream response so SSE and chunked replies survive
        # end-to-end. The httpx `stream` API keeps the connection open
        # until we explicitly read aiter_raw / close.
        try:
            req = client.build_request(
                method,
                upstream_url,
                headers=outbound_headers,
                content=body if body else None,
            )
            stream_ctx = client.stream(
                method,
                upstream_url,
                headers=outbound_headers,
                content=body if body else None,
            )
        except httpx.RequestError as exc:
            resp = JSONResponse(
                {
                    "error": "passthrough upstream unreachable",
                    "backend": spec.name,
                    "upstream": upstream_url,
                    "detail": str(exc),
                },
                status_code=502,
            )
            await resp(scope, receive, send)
            return

        # `req` was built above only to validate the request shape early
        # (httpx.URL parsing, encoding errors). The actual on-wire request
        # happens inside `client.stream(...)`. We discard the prebuilt
        # one so the linter doesn't flag unused vars.
        del req

        try:
            async with stream_ctx as upstream_resp:
                response_headers: list[tuple[bytes, bytes]] = []
                for raw_name, raw_value in upstream_resp.headers.raw:
                    name_lower = raw_name.decode("latin-1").lower()
                    if name_lower in _HOP_BY_HOP or name_lower in _FRAME_DENY_HEADERS:
                        continue
                    response_headers.append((raw_name, raw_value))

                await send({
                    "type": "http.response.start",
                    "status": upstream_resp.status_code,
                    "headers": response_headers,
                })

                # Stream chunks straight through. `aiter_raw` yields the
                # bytes exactly as the upstream sends them, preserving SSE
                # frame boundaries.
                async for chunk in upstream_resp.aiter_raw():
                    if not chunk:
                        continue
                    await send({
                        "type": "http.response.body",
                        "body": chunk,
                        "more_body": True,
                    })
                # Final empty frame to flush.
                await send({
                    "type": "http.response.body",
                    "body": b"",
                    "more_body": False,
                })
        except httpx.RequestError as exc:
            # If the stream errors mid-flight (e.g. upstream TLS issue),
            # we may have already sent a 200 / response.start — in that
            # case the best we can do is close. If not, emit a 502.
            try:
                await send({
                    "type": "http.response.start",
                    "status": 502,
                    "headers": [(b"content-type", b"application/json")],
                })
                await send({
                    "type": "http.response.body",
                    "body": (
                        b'{"error":"passthrough stream failed",'
                        b'"detail":' + repr(str(exc)).encode("utf-8") + b"}"
                    ),
                    "more_body": False,
                })
            except Exception:
                # Already mid-response; nothing to do.
                pass

    def status(self) -> dict[str, Any]:
        servers = []
        for name, state in self.servers.items():
            spec = self._specs.get(name)
            entry: dict[str, Any] = {
                "name": name,
                "running": state.running,
                "error": state.error,
                "primary": name == self._primary,
                # `builtin: true` lets the UI render the always-on row
                # differently (no Stop button, etc.). Both the introspection
                # builtin (`zelosmcp`) and the data-path server (`zelos`) are
                # always-on built-ins.
                "builtin": name in (BUILTIN_NAME, DATAPATH_NAME),
            }
            if spec is not None:
                entry["transport"] = spec.transport
                entry["spec"] = spec.to_status()
            elif state.backend_info:
                entry["transport"] = state.backend_info.get("transport")
                entry["spec"] = dict(state.backend_info)
            # Passthrough surfacing: explicit per-row flag + auth_state +
            # pool stats so the UI can render the right badge and the
            # agent (via `zelosmcp__list_loaded_servers`) can see which
            # auth mode is active. Provider-backed passthrough is broker
            # mode (GUI device flow + token injection), not the legacy
            # "needs inbound Authorization" mode.
            if getattr(state, "is_passthrough", False):
                entry["passthrough"] = True
                provider_name = getattr(spec, "auth_provider", None) if spec else None
                if provider_name:
                    provider = self.auth_registry.get(provider_name)
                    entry["auth_state"] = (
                        "provider_ready" if provider is not None
                        else "provider_missing"
                    )
                    entry["auth_provider"] = provider_name
                elif getattr(state, "passthrough_auth_bearer", None):
                    entry["auth_state"] = "static_bearer"
                else:
                    entry["auth_state"] = "needs_inbound_token"
                pool = getattr(state, "passthrough_pool", None)
                if pool is not None:
                    try:
                        entry["passthrough_pool"] = pool.stats()
                    except Exception:
                        # Pool not fully initialised — surface absence
                        # rather than crashing /api/status.
                        pass
            servers.append(entry)
        # `running` reflects whether any USER backend is up. The builtin
        # is always up by design, so it's excluded from this aggregate
        # so the UI badge / curl probes still mean what they used to.
        result: dict[str, Any] = {
            "primary": self._primary,
            "servers": servers,
            "running": any(
                s.running
                for n, s in self.servers.items()
                if n not in (BUILTIN_NAME, DATAPATH_NAME)
            ),
        }
        # Surface the active BuiltinConfig so config-round-trip tooling
        # (e.g. the benchmark harness) can preserve top-level builtin
        # settings across /api/start calls. Always present (even at
        # defaults) so callers don't have to special-case absence.
        if self._builtin_config is not None:
            result["builtin"] = self._builtin_config.to_status()
        else:
            result["builtin"] = {}
        return result

    def subscribe_logs(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=512)
        self._log_subscribers.append(q)
        return q

    def subscribe_logs_with_history(
        self,
    ) -> tuple[list[str], asyncio.Queue[str]]:
        """Snapshot the buffered history and atomically register a new
        subscriber queue.

        Both operations are synchronous, so they run without any
        ``await`` interleaving with ``_broadcast`` (the only writer of
        history and queues). That means new lines emitted after this
        call go to the queue, lines emitted before went to the
        snapshot, and there is no window for duplicates or drops.
        Callers should drain the snapshot first, then pull from the
        queue.
        """
        snapshot = list(self._log_history)
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=512)
        self._log_subscribers.append(q)
        return snapshot, q

    def unsubscribe_logs(self, q: asyncio.Queue[str]) -> None:
        try:
            self._log_subscribers.remove(q)
        except ValueError:
            pass

    def _broadcast(self, line: str) -> None:
        self._log_history.append(line)
        for q in list(self._log_subscribers):
            try:
                q.put_nowait(line)
            except asyncio.QueueFull:
                pass

    def _broadcast_tagged(self, tag: str, message: str) -> None:
        """Broadcast an activity-log line in the canonical
        ``[HH:MM:SS] [<tag>] <message>`` format.

        Use this for manager-/aggregator-/savings-level events that
        don't originate from a per-backend ``_emit_log`` (which already
        timestamps). Keeps every line in the ``/api/logs`` SSE stream
        and the home-page activity panel uniformly parseable.
        """
        ts = time.strftime("%H:%M:%S")
        self._broadcast(f"[{ts}] [{tag}] {message}")

    def _attach_log_pump(self, state: ProxyState) -> None:
        """Forward a child state's logs into the manager's subscriber set."""
        source = state.subscribe_logs()

        async def pump() -> None:
            try:
                while True:
                    line = await source.get()
                    self._broadcast(line)
            except asyncio.CancelledError:
                pass
            finally:
                state.unsubscribe_logs(source)

        task = asyncio.create_task(pump())
        self._log_pumps[state.name] = task

    async def _start_one_spec(self, state: ProxyState, spec: ServerSpec) -> None:
        await state.start(
            transport=spec.transport,
            command=spec.command,
            args=spec.args if spec.transport == "stdio" else None,
            url=spec.url,
            env=spec.env,
            cwd=spec.cwd,
            headers=spec.headers,
            compress=spec.compress,
            passthrough=spec.passthrough,
            auth_bearer=spec.auth_bearer,
            passthrough_pool=spec.passthrough_pool,
            response_format=spec.response_format,
            strip_meta=spec.strip_meta,
        )
