"""Cursor-compatible MCP config parsing.

Accepts the same ``mcpServers`` shape Cursor uses in ``mcp.json`` and adds an
optional top-level ``primaryMCP`` field naming the server that should also be
mounted at ``/mcp`` (in addition to its always-present ``/<name>/mcp``).

A single ``parse_config`` call returns a list of normalized :class:`ServerSpec`
objects plus the resolved primary name (if any). Reserved path segments are
rejected up front so configs cannot collide with the app's own routes.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


RESERVED_NAMES: frozenset[str] = frozenset({
    "api",
    "mcp",
    "docs",
    "redoc",
    "openapi.json",
    "openapi",
    "static",
    # `zelosmcp` is the always-on built-in backend (BuiltinServer in
    # zelosmcp.builtin); reserved so user configs can't shadow it.
    "zelosmcp",
    # `zelos` is the always-on data-path backend (DataPathServer in
    # zelosmcp.server) mounted at /zelos/mcp; reserved likewise.
    "zelos",
})

# Path prefixes a backend's reverseProxy.mount cannot claim. These either
# host zelosMCP's own surface (/api/*, /docs, /redoc, /openapi.json,
# /catalog) or are reserved by the MCP dispatcher (/, /mcp). Anything not
# in this set is fair game.
RESERVED_MOUNTS: frozenset[str] = frozenset({
    "/",
    "/api",
    "/mcp",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/catalog",
})

# Compression knobs for tool-list shrinking. See docs/compression.md.
COMPRESS_LEVELS: frozenset[str] = frozenset({"low", "medium", "high", "max"})
COMPRESS_SCOPES: frozenset[str] = frozenset({"catalog", "aggregator", "global"})
RESPONSE_FORMATS: frozenset[str] = frozenset({"toon", "compact_json", "raw"})

# Recognised provider types in ``configs/auth-providers.json``. Two are
# legacy shims around pre-existing zelosMCP behaviour
# (``passthrough``/``static``); the other two are real OAuth providers
# implemented in :mod:`zelosmcp.auth.github` and
# :mod:`zelosmcp.auth.okta`. Adding a new provider type means: extend
# this set, add validation in :func:`_parse_auth_provider`, add a
# factory in :mod:`zelosmcp.auth.factory`.
AUTH_PROVIDER_TYPES: frozenset[str] = frozenset({
    "github_device_flow",
    "okta_authorization_code",
    "okta_device_flow",
    "passthrough",
    "static",
})

# Passthrough session-pool defaults. See docs/oauth-passthrough.md. Per-Cursor
# upstream sessions are keyed by SHA-256 of the inbound Authorization header
# value; the pool caps total sessions per backend (LRU eviction) and idle TTL
# so abandoned tokens don't pin connections forever.
PASSTHROUGH_MAX_SESSIONS_DEFAULT: int = 64
PASSTHROUGH_IDLE_TTL_SECONDS_DEFAULT: int = 1800

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.]*$")
_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(ValueError):
    """Raised when a config payload cannot be parsed into ServerSpecs."""


@dataclass
class OpenApiContractSpec:
    """OpenAPI contract exposed by a reverse-proxied backend."""

    path: str

    def to_status(self) -> dict[str, Any]:
        return {"path": self.path}


@dataclass
class ReverseProxySpec:
    """HTTP reverse-proxy configuration for one backend.

    When set, zelosMCP forwards HTTP requests on ``mount`` to ``upstream``,
    injecting a canonical ``X-Forwarded-*`` header set so the upstream knows
    its public-facing path. Lets you expose a backend's HTTP sidecar (e.g.
    pincher's dashboard) through zelosMCP's port without leaking the
    backend's own port.
    """

    mount: str
    upstream: str
    strip_prefix: bool = False
    headers: dict[str, str] = field(default_factory=dict)
    auth_bearer: str | None = None
    openapi: OpenApiContractSpec | None = None

    def to_status(self) -> dict[str, Any]:
        """JSON-serializable view round-tripping the input config shape.

        ``auth.bearer`` is intentionally omitted from the status payload so
        secrets don't leak through ``/api/status``.
        """
        info: dict[str, Any] = {"mount": self.mount, "upstream": self.upstream}
        if self.strip_prefix:
            info["stripPrefix"] = True
        if self.headers:
            info["headers"] = dict(self.headers)
        if self.auth_bearer:
            info["auth"] = {"bearer": "***"}
        if self.openapi is not None:
            info["openapi"] = self.openapi.to_status()
        return info


@dataclass
class CompressSpec:
    """Tool-list compression policy for one backend.

    Replaces the backend's full tool surface with compressed wrappers
    (``get_tool_schema`` + ``search_tools`` + ``invoke_tool``, or a single
    ``list_tools`` at level=max) so the LLM sees a much smaller schema in
    ``tools/list``.

    - ``level`` controls how aggressively the inlined catalog is summarised.
    - ``scope`` controls which endpoints the wrapper replacement applies to:
      ``catalog`` (docs/discovery only), ``aggregator`` (default — replaces
      tools at ``/mcp``), or ``global`` (also replaces at ``/<name>/mcp``).
    """

    level: str = "medium"
    scope: str = "aggregator"

    def to_status(self) -> dict[str, Any]:
        return {"level": self.level, "scope": self.scope}


@dataclass
class PassthroughPoolSpec:
    """Per-backend session-pool sizing for ``passthrough`` HTTP backends.

    The pool caches one upstream :class:`mcp.client.session.ClientSession`
    per inbound Authorization header (keyed by SHA-256 hash) so multiple
    Cursor clients sharing the same OAuth token reuse a single connection.
    Sessions evict on LRU when ``max_sessions`` is hit and on idle TTL.
    """

    max_sessions: int = PASSTHROUGH_MAX_SESSIONS_DEFAULT
    idle_ttl_seconds: int = PASSTHROUGH_IDLE_TTL_SECONDS_DEFAULT

    def to_status(self) -> dict[str, Any]:
        return {
            "maxSessions": self.max_sessions,
            "idleTtlSeconds": self.idle_ttl_seconds,
        }


@dataclass
class AuthProviderSpec:
    """One provider definition parsed from ``configs/auth-providers.json``.

    Pure data — turning a spec into a working
    :class:`zelosmcp.auth.protocol.AuthProvider` instance happens in
    :mod:`zelosmcp.auth.factory`. The two halves are split because
    config parsing has to validate at startup before the auth_store
    is open (the store is what real providers need to construct).

    Field semantics by ``type``:

    - ``github_device_flow`` — requires ``client_id``, optional
      ``scopes``. No ``bearer``, no ``issuer``.
    - ``okta_device_flow`` — requires ``issuer`` + ``client_id``,
      optional ``scopes``, optional ``membership_hint``.
    - ``okta_authorization_code`` — Okta Authorization Code + PKCE for
      Native apps. Requires ``issuer`` + ``client_id``; optional
      ``redirect_uri`` (defaults locally), ``scopes`` and
      ``membership_hint``.
    - ``passthrough`` — only ``name`` + ``type``; all other fields
      rejected. Wraps the legacy "forward Authorization verbatim"
      behaviour as an :class:`AuthProvider`.
    - ``static`` — requires ``bearer`` (env-interpolated). Wraps the
      legacy ``auth.bearer`` static-token mode.

    ``membership_hint`` is universally optional regardless of type —
    a free-form display string the GUI surfaces on the provider card
    (e.g. ``"Membership required: Nike.uee.maria"``). Never used for
    authorization.
    """

    name: str
    type: str
    client_id: str | None = None
    client_secret: str | None = None
    issuer: str | None = None
    redirect_uri: str | None = None
    scopes: list[str] = field(default_factory=list)
    membership_hint: str | None = None
    bearer: str | None = None  # static only

    def to_status(self, *, redacted: bool = True) -> dict[str, Any]:
        """JSON-serialisable view used by ``GET /api/auth/providers/config``.

        ``client_id`` is non-sensitive (public OAuth-client identifier
        that ships in zelosMCP's default config) and stays in the
        clear. ``bearer`` is always redacted. ``membership_hint`` is
        a free-form display string with no secret content.
        """
        info: dict[str, Any] = {"name": self.name, "type": self.type}
        if self.client_id:
            info["client_id"] = self.client_id
        if self.client_secret:
            info["client_secret"] = "***" if redacted else self.client_secret
        if self.issuer:
            info["issuer"] = self.issuer
        if self.redirect_uri:
            info["redirect_uri"] = self.redirect_uri
        if self.scopes:
            info["scopes"] = list(self.scopes)
        if self.membership_hint:
            info["membership_hint"] = self.membership_hint
        if self.bearer:
            info["bearer"] = "***" if redacted else self.bearer
        return info


@dataclass
class BuiltinConfig:
    """Configuration for the always-on ``zelosmcp`` builtin backend.

    Parsed from the optional top-level ``"builtin"`` key in the config
    JSON (alongside ``"mcpServers"``).  Defaults match the pre-feature
    behaviour: ``response_format="raw"`` and compression off.

    Env-var override: ``ZELOSMCP_BUILTIN_RESPONSE_FORMAT``.
    """

    response_format: str = "raw"
    strip_meta: bool = True
    compress: CompressSpec | None = None

    def to_status(self) -> dict[str, Any]:
        info: dict[str, Any] = {}
        if self.response_format != "raw":
            info["response_format"] = self.response_format
        if not self.strip_meta:
            info["strip_meta"] = False
        if self.compress is not None:
            info["compress"] = self.compress.to_status()
        return info


@dataclass
class ServerSpec:
    """Normalized, transport-tagged description of one MCP backend."""

    name: str
    transport: str  # "stdio" | "sse" | "http"
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    reverse_proxy: ReverseProxySpec | None = None
    compress: CompressSpec | None = None
    # OAuth-passthrough mode: zelosMCP forwards HTTP requests to ``url``
    # without owning an MCP session of its own. Inbound Authorization is
    # forwarded verbatim; 401 + WWW-Authenticate from upstream is propagated
    # so the Cursor client (or whichever MCP client) handles the OAuth
    # dance directly with the upstream issuer. See docs/oauth-passthrough.md.
    passthrough: bool = False
    # Static fallback bearer token. Injected on outbound requests ONLY when
    # the inbound request has no Authorization header. Useful for headless
    # / CI scenarios where the OAuth dance isn't possible. Only meaningful
    # when ``passthrough=True``.
    auth_bearer: str | None = None
    # Passthrough session-pool sizing (Phase 2 aggregator integration). Only
    # meaningful when ``passthrough=True``; ignored otherwise.
    passthrough_pool: PassthroughPoolSpec | None = None
    # Modern auth-provider reference. When set, the backend's outbound
    # Authorization header is minted by the named provider instead of
    # forwarded from the inbound request. References a key in the
    # parallel ``configs/auth-providers.json`` file; cross-validation
    # happens after both files load (see
    # :func:`validate_provider_references`). Only meaningful when
    # ``passthrough=True`` because zelosMCP can't intercept inbound
    # Authorization on session-bound backends.
    auth_provider: str | None = None
    # Provider-specific audience claim, e.g. ``"api://atlassian-mcp"``
    # for an Okta token-exchange provider. Most providers ignore this
    # (they mint a single token regardless of audience). ``None`` means
    # "let the provider use its default audience".
    auth_audience: str | None = None
    # Response serialization format. Controls how ``TextContent`` blocks
    # in tool-call responses are transformed before returning to the
    # client. ``"toon"`` converts JSON/YAML to Token-Optimized Object
    # Notation for ~40-60% token savings; ``"compact_json"`` minifies
    # JSON; ``"raw"`` passes through unchanged. Env-var override:
    # ``ZELOSMCP_RESPONSE_FORMAT``.
    response_format: str = "toon"
    # Strip ``_meta`` / ``meta`` keys from JSON response blocks before
    # returning to the client. These metadata envelopes (common in
    # pincher responses) consume 40-80% of small payloads with no LLM
    # value. Defaults to ``True``; set ``False`` to preserve them.
    # Env-var override: ``ZELOSMCP_STRIP_META``.
    strip_meta: bool = True
    # Whether this backend should be started when the config loads.
    # ``True`` (the default) starts the backend immediately;
    # ``False`` installs (registers the spec, creates a ProxyState)
    # but leaves it stopped — excluded from tools/list and generated
    # rules. Assets can still be edited for stopped backends. The
    # backend can be started later via the GUI or
    # ``zelosmcp__start_server``.
    started: bool = True

    def to_status(self) -> dict[str, Any]:
        """Compact JSON-serializable view used by status endpoints."""
        info: dict[str, Any] = {"name": self.name, "transport": self.transport}
        if self.command:
            info["command"] = self.command
            if self.args:
                info["args"] = list(self.args)
            if self.env:
                info["env"] = dict(self.env)
            if self.cwd:
                info["cwd"] = self.cwd
        if self.url:
            info["url"] = self.url
            if self.headers:
                info["headers"] = dict(self.headers)
        if self.reverse_proxy is not None:
            info["reverseProxy"] = self.reverse_proxy.to_status()
        if self.compress is not None:
            info["compress"] = self.compress.to_status()
        if self.passthrough:
            info["passthrough"] = True
        # Compose the ``auth`` block from whichever fields are present.
        # ``bearer`` and ``provider`` are mutually exclusive (enforced
        # in :func:`_parse_top_level_auth`); both can't appear here.
        auth_block: dict[str, Any] = {}
        if self.auth_bearer:
            # Mirrors ReverseProxySpec.to_status — never leak secrets.
            auth_block["bearer"] = "***"
        if self.auth_provider:
            auth_block["provider"] = self.auth_provider
            if self.auth_audience:
                auth_block["audience"] = self.auth_audience
        if auth_block:
            info["auth"] = auth_block
        if self.passthrough_pool is not None:
            info["passthroughPool"] = self.passthrough_pool.to_status()
        if self.response_format != "toon":
            info["response_format"] = self.response_format
        if not self.strip_meta:
            info["strip_meta"] = False
        if not self.started:
            info["started"] = False
        return info


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not name:
        raise ConfigError("Server names must be non-empty strings")
    if name.lower() in RESERVED_NAMES:
        raise ConfigError(
            f"Server name '{name}' is reserved (collides with a built-in route)"
        )
    if not _NAME_RE.match(name):
        raise ConfigError(
            f"Server name '{name}' is invalid: use letters, digits, '-', '_', or '.'"
        )


def _coerce_str_dict(value: Any, field_name: str, server_name: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ConfigError(
            f"Server '{server_name}': '{field_name}' must be an object of strings"
        )
    out: dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ConfigError(
                f"Server '{server_name}': '{field_name}' entries must be string→string"
            )
        out[k] = v
    return out


def _validate_mount(server_name: str, mount: Any) -> str:
    """Normalize and validate a reverse-proxy mount path.

    Returns the canonical form (leading slash, no trailing slash). Rejects
    values that would shadow zelosMCP's own routes or the MCP dispatcher.
    """
    if not isinstance(mount, str) or not mount:
        raise ConfigError(
            f"Server '{server_name}': reverseProxy.mount must be a non-empty string"
        )
    if not mount.startswith("/"):
        raise ConfigError(
            f"Server '{server_name}': reverseProxy.mount must start with '/' (got {mount!r})"
        )
    if any(ch.isspace() for ch in mount):
        raise ConfigError(
            f"Server '{server_name}': reverseProxy.mount must not contain whitespace"
        )
    if ".." in mount:
        raise ConfigError(
            f"Server '{server_name}': reverseProxy.mount must not contain '..'"
        )
    if mount != "/" and mount.endswith("/"):
        raise ConfigError(
            f"Server '{server_name}': reverseProxy.mount must not end with '/' (got {mount!r})"
        )
    if mount in RESERVED_MOUNTS:
        raise ConfigError(
            f"Server '{server_name}': reverseProxy.mount '{mount}' is reserved "
            "(collides with a built-in route)"
        )
    return mount


def _parse_reverse_proxy_openapi(
    server_name: str, raw: Any
) -> OpenApiContractSpec:
    """Validate and normalize a ``reverseProxy.openapi`` block."""
    if not isinstance(raw, dict):
        raise ConfigError(
            f"Server '{server_name}': reverseProxy.openapi must be an object"
        )
    path = raw.get("path")
    if not isinstance(path, str) or not path:
        raise ConfigError(
            f"Server '{server_name}': reverseProxy.openapi.path must be a "
            "non-empty string"
        )
    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc:
        raise ConfigError(
            f"Server '{server_name}': reverseProxy.openapi.path must be a "
            f"path relative to reverseProxy.upstream, not a URL (got {path!r})"
        )
    if not path.startswith("/"):
        raise ConfigError(
            f"Server '{server_name}': reverseProxy.openapi.path must start "
            f"with '/' (got {path!r})"
        )
    if any(ch.isspace() for ch in path):
        raise ConfigError(
            f"Server '{server_name}': reverseProxy.openapi.path must not "
            "contain whitespace"
        )
    if ".." in path:
        raise ConfigError(
            f"Server '{server_name}': reverseProxy.openapi.path must not "
            "contain '..'"
        )
    return OpenApiContractSpec(path=path)


def load_secret_env(var: str) -> str | None:
    """Standard suite secret lookup honoring the container contract.

    Order of resolution (see zelosai/docs/architecture/07):
      1. ``$VAR``        — direct env (allowed for local dev).
      2. ``$VAR_FILE``   — path to a Secret-mounted file at
         ``/etc/zelos/secrets/<key>`` (the only allowed form in-cluster).

    Returns ``None`` when neither is set so callers can distinguish "unset"
    from "set but empty".
    """
    direct = os.environ.get(var)
    if direct is not None:
        return direct
    path = os.environ.get(var + "_FILE")
    if path:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().rstrip("\n")
        except OSError:
            return None
    return None


def _interpolate_env(value: str, server_name: str, field_name: str) -> str:
    """Replace ``${VAR}`` substrings with values from the environment.

    Honors the standard secret lookup (``$VAR`` → ``$VAR_FILE``). Raises
    :class:`ConfigError` when a referenced variable is unset, so misconfigured
    deployments fail loudly at parse time instead of silently forwarding
    ``Authorization: Bearer ${PINCHER_HTTP_KEY}`` literally.
    """
    def _replace(match: re.Match[str]) -> str:
        var = match.group(1)
        env_val = load_secret_env(var)
        if env_val is None:
            raise ConfigError(
                f"Server '{server_name}': {field_name} references "
                f"${{{var}}} but the environment variable is not set"
            )
        return env_val

    return _ENV_VAR_RE.sub(_replace, value)


def _parse_reverse_proxy(server_name: str, raw: Any) -> ReverseProxySpec:
    """Validate and normalize one ``reverseProxy`` block."""
    if not isinstance(raw, dict):
        raise ConfigError(
            f"Server '{server_name}': 'reverseProxy' must be an object"
        )

    mount = _validate_mount(server_name, raw.get("mount"))

    upstream = raw.get("upstream")
    if not isinstance(upstream, str) or not upstream:
        raise ConfigError(
            f"Server '{server_name}': reverseProxy.upstream must be a non-empty string"
        )
    parsed = urlparse(upstream)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(
            f"Server '{server_name}': reverseProxy.upstream must be an "
            f"http:// or https:// URL with a host (got {upstream!r})"
        )

    strip_prefix_raw = raw.get("stripPrefix", False)
    if not isinstance(strip_prefix_raw, bool):
        raise ConfigError(
            f"Server '{server_name}': reverseProxy.stripPrefix must be a boolean"
        )

    headers: dict[str, str] = {}
    if "headers" in raw and raw["headers"] is not None:
        headers = _coerce_str_dict(raw["headers"], "reverseProxy.headers", server_name)

    auth_bearer: str | None = None
    auth_raw = raw.get("auth")
    if auth_raw is not None:
        if not isinstance(auth_raw, dict):
            raise ConfigError(
                f"Server '{server_name}': reverseProxy.auth must be an object"
            )
        bearer_raw = auth_raw.get("bearer")
        if bearer_raw is not None:
            if not isinstance(bearer_raw, str):
                raise ConfigError(
                    f"Server '{server_name}': reverseProxy.auth.bearer must be a string"
                )
            auth_bearer = _interpolate_env(
                bearer_raw, server_name, "reverseProxy.auth.bearer"
            )

    openapi: OpenApiContractSpec | None = None
    if "openapi" in raw and raw["openapi"] is not None:
        openapi = _parse_reverse_proxy_openapi(server_name, raw["openapi"])

    return ReverseProxySpec(
        mount=mount.rstrip("/") if mount != "/" else mount,
        upstream=upstream.rstrip("/"),
        strip_prefix=strip_prefix_raw,
        headers=headers,
        auth_bearer=auth_bearer,
        openapi=openapi,
    )


def _parse_passthrough_pool(server_name: str, raw: Any) -> PassthroughPoolSpec:
    """Validate and normalize one ``passthroughPool`` block.

    Both fields are optional; an empty object yields all dataclass defaults.
    Negative or zero values are rejected so a misconfig can't disable the
    pool's eviction behaviour silently.
    """
    if not isinstance(raw, dict):
        raise ConfigError(
            f"Server '{server_name}': 'passthroughPool' must be an object"
        )

    max_sessions = raw.get("maxSessions", PASSTHROUGH_MAX_SESSIONS_DEFAULT)
    if not isinstance(max_sessions, int) or isinstance(max_sessions, bool) or max_sessions <= 0:
        raise ConfigError(
            f"Server '{server_name}': passthroughPool.maxSessions must be "
            f"a positive integer (got {max_sessions!r})"
        )

    idle_ttl = raw.get("idleTtlSeconds", PASSTHROUGH_IDLE_TTL_SECONDS_DEFAULT)
    if not isinstance(idle_ttl, int) or isinstance(idle_ttl, bool) or idle_ttl <= 0:
        raise ConfigError(
            f"Server '{server_name}': passthroughPool.idleTtlSeconds must be "
            f"a positive integer (got {idle_ttl!r})"
        )

    return PassthroughPoolSpec(max_sessions=max_sessions, idle_ttl_seconds=idle_ttl)


def _parse_compress(server_name: str, raw: Any) -> CompressSpec:
    """Validate and normalize one ``compress`` block.

    Both ``level`` and ``scope`` default to the dataclass defaults
    (``medium`` / ``aggregator``) when the keys are absent. An empty
    object ``{}`` therefore expands to a fully-default-configured
    :class:`CompressSpec` rather than being rejected.
    """
    if not isinstance(raw, dict):
        raise ConfigError(
            f"Server '{server_name}': 'compress' must be an object"
        )

    level = raw.get("level", "medium")
    if not isinstance(level, str) or level not in COMPRESS_LEVELS:
        raise ConfigError(
            f"Server '{server_name}': compress.level must be one of "
            f"{sorted(COMPRESS_LEVELS)} (got {level!r})"
        )

    scope = raw.get("scope", "aggregator")
    if not isinstance(scope, str) or scope not in COMPRESS_SCOPES:
        raise ConfigError(
            f"Server '{server_name}': compress.scope must be one of "
            f"{sorted(COMPRESS_SCOPES)} (got {scope!r})"
        )

    return CompressSpec(level=level, scope=scope)


def _parse_top_level_auth(
    name: str, raw: Any
) -> tuple[str | None, str | None, str | None]:
    """Parse the top-level ``auth`` block on one server entry.

    Returns ``(bearer, provider, audience)`` — at most one of
    ``bearer`` / ``provider`` is set. Both is a config error so a
    misconfig can't silently drop one field's intent.

    - ``auth: { bearer: "${VAR}" }`` — legacy static-token mode.
      Equivalent to a synthetic ``static`` provider auto-registered
      by the manager.
    - ``auth: { provider: "github_oauth_app" }`` — modern provider
      reference. Optional ``audience`` field for token-exchange
      style providers.
    """
    auth_raw = raw.get("auth")
    if auth_raw is None:
        return None, None, None
    if not isinstance(auth_raw, dict):
        raise ConfigError(f"Server '{name}': 'auth' must be an object")

    bearer_raw = auth_raw.get("bearer")
    provider_raw = auth_raw.get("provider")
    audience_raw = auth_raw.get("audience")

    if bearer_raw is not None and provider_raw is not None:
        raise ConfigError(
            f"Server '{name}': 'auth' must specify either 'bearer' OR "
            "'provider', not both. Use 'provider' to reference an entry "
            "in configs/auth-providers.json; use 'bearer' for the legacy "
            "static-token mode."
        )

    bearer: str | None = None
    if bearer_raw is not None:
        if not isinstance(bearer_raw, str):
            raise ConfigError(f"Server '{name}': auth.bearer must be a string")
        bearer = _interpolate_env(bearer_raw, name, "auth.bearer")

    provider: str | None = None
    if provider_raw is not None:
        if not isinstance(provider_raw, str) or not provider_raw:
            raise ConfigError(
                f"Server '{name}': auth.provider must be a non-empty string"
            )
        if not _NAME_RE.match(provider_raw):
            raise ConfigError(
                f"Server '{name}': auth.provider name '{provider_raw}' is "
                "invalid: use letters, digits, '-', '_', or '.'"
            )
        provider = provider_raw

    audience: str | None = None
    if audience_raw is not None:
        if provider is None:
            raise ConfigError(
                f"Server '{name}': auth.audience is only valid alongside "
                "auth.provider"
            )
        if not isinstance(audience_raw, str) or not audience_raw:
            raise ConfigError(
                f"Server '{name}': auth.audience must be a non-empty string"
            )
        audience = audience_raw

    return bearer, provider, audience


def _parse_auth_provider(name: str, raw: Any) -> AuthProviderSpec:
    """Validate and normalize one entry from
    ``configs/auth-providers.json``'s ``providers`` mapping.

    Type-specific required fields are enforced here; cross-cutting
    field validation (env interpolation, name format) is shared.
    """
    if not isinstance(raw, dict):
        raise ConfigError(
            f"Auth provider '{name}': entry must be an object"
        )
    _validate_name(name)

    type_raw = raw.get("type")
    if not isinstance(type_raw, str) or type_raw not in AUTH_PROVIDER_TYPES:
        raise ConfigError(
            f"Auth provider '{name}': 'type' must be one of "
            f"{sorted(AUTH_PROVIDER_TYPES)} (got {type_raw!r})"
        )

    # Per-type field allowlist + required fields.
    allowed: set[str] = {"name", "type", "membership_hint"}
    required: set[str] = set()
    if type_raw == "github_device_flow":
        allowed |= {"client_id", "scopes"}
        required = {"client_id"}
    elif type_raw == "okta_device_flow":
        allowed |= {"client_id", "issuer", "scopes"}
        required = {"client_id", "issuer"}
    elif type_raw == "okta_authorization_code":
        allowed |= {
            "client_id",
            "client_secret",
            "issuer",
            "redirect_uri",
            "scopes",
        }
        required = {"client_id", "issuer"}
    elif type_raw == "static":
        allowed |= {"bearer"}
        required = {"bearer"}
    # passthrough has only the cross-cutting fields; no extras allowed.

    extra_keys = set(raw) - allowed
    if extra_keys:
        raise ConfigError(
            f"Auth provider '{name}' (type {type_raw!r}): unrecognised "
            f"fields {sorted(extra_keys)}. Allowed: {sorted(allowed)}"
        )
    missing = required - set(raw)
    if missing:
        raise ConfigError(
            f"Auth provider '{name}' (type {type_raw!r}): missing required "
            f"field(s) {sorted(missing)}"
        )

    client_id: str | None = None
    if "client_id" in raw:
        if not isinstance(raw["client_id"], str) or not raw["client_id"]:
            raise ConfigError(
                f"Auth provider '{name}': client_id must be a non-empty string"
            )
        client_id = _interpolate_env(raw["client_id"], name, "client_id")

    client_secret: str | None = None
    if "client_secret" in raw:
        if not isinstance(raw["client_secret"], str) or not raw["client_secret"]:
            raise ConfigError(
                f"Auth provider '{name}': client_secret must be a non-empty string"
            )
        client_secret = _interpolate_env(
            raw["client_secret"], name, "client_secret"
        )

    issuer: str | None = None
    if "issuer" in raw:
        if not isinstance(raw["issuer"], str) or not raw["issuer"]:
            raise ConfigError(
                f"Auth provider '{name}': issuer must be a non-empty string"
            )
        issuer_str = _interpolate_env(raw["issuer"], name, "issuer")
        parsed = urlparse(issuer_str)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ConfigError(
                f"Auth provider '{name}': issuer must be an http:// or "
                f"https:// URL with a host (got {issuer_str!r})"
            )
        issuer = issuer_str

    redirect_uri: str | None = None
    if "redirect_uri" in raw:
        if not isinstance(raw["redirect_uri"], str) or not raw["redirect_uri"]:
            raise ConfigError(
                f"Auth provider '{name}': redirect_uri must be a non-empty string"
            )
        redirect_uri_str = _interpolate_env(
            raw["redirect_uri"], name, "redirect_uri"
        )
        parsed = urlparse(redirect_uri_str)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ConfigError(
                f"Auth provider '{name}': redirect_uri must be an http:// or "
                f"https:// URL with a host (got {redirect_uri_str!r})"
            )
        redirect_uri = redirect_uri_str

    scopes: list[str] = []
    if "scopes" in raw:
        scopes_raw = raw["scopes"]
        if not isinstance(scopes_raw, list) or not all(
            isinstance(s, str) and s for s in scopes_raw
        ):
            raise ConfigError(
                f"Auth provider '{name}': scopes must be an array of "
                "non-empty strings"
            )
        scopes = list(scopes_raw)

    membership_hint: str | None = None
    if "membership_hint" in raw:
        hint_raw = raw["membership_hint"]
        if hint_raw is not None:
            if not isinstance(hint_raw, str):
                raise ConfigError(
                    f"Auth provider '{name}': membership_hint must be a string"
                )
            # Empty after env interpolation is treated as unset so a
            # missing env var doesn't surface "Membership required: " to
            # the UI with a blank tail.
            interpolated = _interpolate_env(
                hint_raw, name, "membership_hint"
            ).strip()
            membership_hint = interpolated or None

    bearer: str | None = None
    if "bearer" in raw:
        if not isinstance(raw["bearer"], str) or not raw["bearer"]:
            raise ConfigError(
                f"Auth provider '{name}': bearer must be a non-empty string"
            )
        bearer = _interpolate_env(raw["bearer"], name, "bearer")

    return AuthProviderSpec(
        name=name,
        type=type_raw,
        client_id=client_id,
        client_secret=client_secret,
        issuer=issuer,
        redirect_uri=redirect_uri,
        scopes=scopes,
        membership_hint=membership_hint,
        bearer=bearer,
    )


def parse_auth_providers(raw: Any) -> dict[str, AuthProviderSpec]:
    """Parse the ``configs/auth-providers.json`` file body.

    Top-level shape: ``{"providers": {<name>: {<spec>, ...}, ...}}``.
    Returns a name-keyed dict matching the input order. The empty
    object ``{"providers": {}}`` is allowed and yields an empty
    dict (a deployment with zero providers configured); the missing
    ``providers`` key is also tolerated for ergonomics.

    Validation is per-provider (see :func:`_parse_auth_provider`)
    plus duplicate-name detection (case-insensitive). Cross-validation
    against backend specs lives in
    :func:`validate_provider_references` and runs once both files
    have been parsed.
    """
    if not isinstance(raw, dict):
        raise ConfigError("Auth providers config must be a JSON object")

    providers_raw = raw.get("providers")
    if providers_raw is None:
        return {}
    if not isinstance(providers_raw, dict):
        raise ConfigError(
            "'providers' must be an object mapping name -> provider config"
        )

    out: dict[str, AuthProviderSpec] = {}
    seen: set[str] = set()
    for name, entry in providers_raw.items():
        spec = _parse_auth_provider(name, entry)
        lower = name.lower()
        if lower in seen:
            raise ConfigError(
                f"Duplicate auth provider name (case-insensitive): '{name}'"
            )
        seen.add(lower)
        out[name] = spec
    return out


def validate_provider_references(
    server_specs: list[ServerSpec],
    provider_specs: dict[str, AuthProviderSpec],
) -> None:
    """Cross-check that every backend's ``auth.provider`` reference
    resolves to an entry in the providers config.

    Called from the manager after both files have been parsed (or
    after the live config-replace endpoint swaps either set). Raises
    :class:`ConfigError` on the first dangling reference; the error
    message lists both the backend name and the missing provider so
    the user can fix the typo without grep-ing the providers file.
    """
    for server in server_specs:
        if server.auth_provider is None:
            continue
        if server.auth_provider not in provider_specs:
            available = ", ".join(sorted(provider_specs)) or "(none)"
            raise ConfigError(
                f"Server '{server.name}' references auth provider "
                f"'{server.auth_provider}' which is not defined in the "
                f"auth-providers config. Available: {available}"
            )


def _parse_server(name: str, raw: Any) -> ServerSpec:
    if not isinstance(raw, dict):
        raise ConfigError(f"Server '{name}': entry must be an object")

    declared_type = raw.get("type")

    reverse_proxy: ReverseProxySpec | None = None
    if "reverseProxy" in raw and raw["reverseProxy"] is not None:
        reverse_proxy = _parse_reverse_proxy(name, raw["reverseProxy"])

    # Passthrough flag — only valid for HTTP transports. Validated below per
    # transport branch so the error message can reference the right field.
    passthrough_raw = raw.get("passthrough", False)
    if not isinstance(passthrough_raw, bool):
        raise ConfigError(
            f"Server '{name}': 'passthrough' must be a boolean (got {passthrough_raw!r})"
        )

    # Top-level auth block. Yields (bearer, provider, audience) — at
    # most one of bearer/provider is non-None (parser enforces). Both
    # are only meaningful for passthrough HTTP backends; rejected below
    # when set on stdio or non-passthrough HTTP.
    top_level_auth_bearer, auth_provider, auth_audience = _parse_top_level_auth(
        name, raw
    )

    # Pool sizing — only honoured when passthrough=True; rejected on stdio
    # / non-passthrough so misconfigs surface immediately.
    passthrough_pool: PassthroughPoolSpec | None = None
    if "passthroughPool" in raw and raw["passthroughPool"] is not None:
        passthrough_pool = _parse_passthrough_pool(name, raw["passthroughPool"])

    # Default-on: every backend gets `medium` compression scoped to the
    # aggregator unless it explicitly opts out. Opt-out forms:
    #   "compress": null        # disable entirely
    #   "compress": false       # disable entirely (legacy convenience)
    # Opt-in / override forms:
    #   (key omitted)           # CompressSpec(level=medium, scope=aggregator)
    #   "compress": {}          # same as omitted — dataclass defaults
    #   "compress": {"level": "high"}             # override one field
    #   "compress": {"level": "low", "scope": "global"}
    #
    # Passthrough backends are compressed by default (same as session-bound
    # ones) — the aggregator emits compressed wrappers regardless of inbound
    # auth so the agent always sees a stable surface; the FIRST wrapper
    # invocation drives the upstream OAuth dance via the existing
    # PassthroughChallengeError path. See docs/oauth-passthrough.md.
    # `scope=global` is still rejected for passthrough below because the
    # `/<name>/mcp` path is a streaming reverse proxy and can't host
    # wrappers; only the aggregator at `/mcp` can.
    compress: CompressSpec | None
    if "compress" not in raw:
        compress = CompressSpec()
    elif raw["compress"] is None or raw["compress"] is False:
        compress = None
    else:
        compress = _parse_compress(name, raw["compress"])

    # Started flag: defaults to True (start on config load).
    started_raw = raw.get("started", True)
    if not isinstance(started_raw, bool):
        raise ConfigError(
            f"Server '{name}': 'started' must be a boolean"
        )

    # Response format: per-backend override, env-var fallback, then default.
    response_format_raw = raw.get("response_format")
    if response_format_raw is None:
        response_format_raw = os.environ.get(
            "ZELOSMCP_RESPONSE_FORMAT", "toon"
        )
    if response_format_raw not in RESPONSE_FORMATS:
        raise ConfigError(
            f"Server '{name}': 'response_format' must be one of "
            f"{sorted(RESPONSE_FORMATS)}, got {response_format_raw!r}"
        )

    # Strip _meta / meta from JSON response blocks.
    strip_meta_raw = raw.get("strip_meta")
    if strip_meta_raw is None:
        strip_meta_env = os.environ.get("ZELOSMCP_STRIP_META")
        strip_meta_raw = (
            strip_meta_env.lower() not in ("0", "false", "no")
            if strip_meta_env is not None
            else True
        )
    if not isinstance(strip_meta_raw, bool):
        raise ConfigError(
            f"Server '{name}': 'strip_meta' must be a boolean"
        )

    # Stdio: presence of `command` (matches Cursor's discrimination rule).
    if "command" in raw:
        if passthrough_raw:
            raise ConfigError(
                f"Server '{name}': 'passthrough' is only valid for HTTP "
                "transports (type: streamable-http) — stdio backends already "
                "run as zelosMCP-owned subprocesses."
            )
        if top_level_auth_bearer is not None:
            raise ConfigError(
                f"Server '{name}': top-level 'auth.bearer' is only valid for "
                "passthrough HTTP backends; stdio backends should pass tokens "
                "via 'env' instead."
            )
        if auth_provider is not None:
            raise ConfigError(
                f"Server '{name}': top-level 'auth.provider' is only valid "
                "for passthrough HTTP backends; stdio backends should pass "
                "tokens via 'env' instead."
            )
        if passthrough_pool is not None:
            raise ConfigError(
                f"Server '{name}': 'passthroughPool' is only valid for "
                "passthrough HTTP backends."
            )

        command = raw.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ConfigError(f"Server '{name}': 'command' must be a non-empty string")

        args_raw = raw.get("args", [])
        if not isinstance(args_raw, list) or not all(isinstance(a, str) for a in args_raw):
            raise ConfigError(f"Server '{name}': 'args' must be an array of strings")

        env = None
        if "env" in raw and raw["env"] is not None:
            env = _coerce_str_dict(raw["env"], "env", name)

        cwd = raw.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            raise ConfigError(f"Server '{name}': 'cwd' must be a string")

        return ServerSpec(
            name=name,
            transport="stdio",
            command=command.strip(),
            args=list(args_raw),
            env=env,
            cwd=cwd,
            reverse_proxy=reverse_proxy,
            compress=compress,
            response_format=response_format_raw,
            strip_meta=strip_meta_raw,
            started=started_raw,
        )

    # Remote transports: discriminated by `type`.
    if declared_type in ("sse", "streamable-http"):
        url = raw.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ConfigError(
                f"Server '{name}': '{declared_type}' config requires a 'url' string"
            )

        headers = None
        if "headers" in raw and raw["headers"] is not None:
            headers = _coerce_str_dict(raw["headers"], "headers", name)

        transport = "sse" if declared_type == "sse" else "http"

        if passthrough_raw and transport != "http":
            raise ConfigError(
                f"Server '{name}': 'passthrough' is only valid for "
                "type: streamable-http (got type: sse). The legacy SSE "
                "transport is not supported for passthrough."
            )

        if not passthrough_raw and top_level_auth_bearer is not None:
            raise ConfigError(
                f"Server '{name}': top-level 'auth.bearer' is only valid "
                "when 'passthrough: true' is also set. For non-passthrough "
                "HTTP backends, supply tokens via 'headers' instead."
            )

        if not passthrough_raw and auth_provider is not None:
            raise ConfigError(
                f"Server '{name}': top-level 'auth.provider' is only valid "
                "when 'passthrough: true' is also set. The provider needs "
                "to mint tokens per-request, which only works for passthrough "
                "HTTP backends; for non-passthrough HTTP, supply tokens via "
                "'headers' instead."
            )

        if not passthrough_raw and passthrough_pool is not None:
            raise ConfigError(
                f"Server '{name}': 'passthroughPool' is only valid when "
                "'passthrough: true' is also set."
            )

        # `scope=global` requires zelosMCP to terminate MCP on the
        # `/<name>/mcp` path so the per-backend session manager can host
        # wrappers there. Passthrough backends route `/<name>/mcp` through
        # a streaming HTTP reverse proxy instead, so there's no place to
        # plug wrappers in. Reject explicitly so misconfigs surface at
        # parse time rather than mysteriously failing at runtime.
        # `scope=aggregator` (the default) and `scope=catalog` both work
        # — they're served by the aggregator at `/mcp`, not by the
        # per-backend mount.
        if (
            passthrough_raw
            and compress is not None
            and compress.scope == "global"
        ):
            raise ConfigError(
                f"Server '{name}': 'compress.scope' cannot be 'global' "
                "when 'passthrough: true'. The /<name>/mcp route for "
                "passthrough backends is a streaming reverse proxy that "
                "doesn't terminate MCP, so wrappers can't be served "
                "there. Use 'aggregator' (the default) or 'catalog'."
            )

        return ServerSpec(
            name=name,
            transport=transport,
            url=url.strip(),
            headers=headers,
            reverse_proxy=reverse_proxy,
            compress=compress,
            passthrough=passthrough_raw,
            auth_bearer=top_level_auth_bearer,
            passthrough_pool=passthrough_pool,
            auth_provider=auth_provider,
            auth_audience=auth_audience,
            response_format=response_format_raw,
            strip_meta=strip_meta_raw,
            started=started_raw,
        )

    raise ConfigError(
        f"Server '{name}': could not determine transport. "
        "Provide 'command' (stdio) or 'type' set to 'sse' or 'streamable-http'."
    )


def _check_mount_overlap(specs: list[ServerSpec]) -> None:
    """Reject configs where two backends' mounts overlap.

    A mount ``a`` overlaps ``b`` if either is a prefix of the other (treating
    them as path segments, so ``/foo`` overlaps ``/foo/bar`` but not
    ``/foobar``). The first conflicting pair raises :class:`ConfigError`.
    """
    mounts: list[tuple[str, str]] = [
        (s.name, s.reverse_proxy.mount) for s in specs if s.reverse_proxy is not None
    ]
    for i, (name_a, mount_a) in enumerate(mounts):
        for name_b, mount_b in mounts[i + 1 :]:
            if mount_a == mount_b:
                raise ConfigError(
                    f"reverseProxy mount '{mount_a}' is claimed by both "
                    f"'{name_a}' and '{name_b}'"
                )
            # Segment-aware prefix check: '/foo' overlaps '/foo/bar' but
            # NOT '/foobar'. Append '/' so 'startswith' tests segment
            # boundaries correctly.
            longer, shorter = (
                (mount_a, mount_b) if len(mount_a) > len(mount_b) else (mount_b, mount_a)
            )
            if longer.startswith(shorter + "/"):
                raise ConfigError(
                    f"reverseProxy mounts '{mount_a}' ('{name_a}') and "
                    f"'{mount_b}' ('{name_b}') overlap"
                )


def _parse_builtin_config(raw: Any) -> BuiltinConfig:
    """Parse the optional ``"builtin"`` top-level config key."""
    if raw is None:
        _sm_env = os.environ.get("ZELOSMCP_STRIP_META")
        return BuiltinConfig(
            response_format=os.environ.get(
                "ZELOSMCP_BUILTIN_RESPONSE_FORMAT", "raw"
            ),
            strip_meta=(
                _sm_env.lower() not in ("0", "false", "no")
                if _sm_env is not None
                else True
            ),
        )
    if not isinstance(raw, dict):
        raise ConfigError("'builtin' must be an object")

    rf = raw.get("response_format")
    if rf is None:
        rf = os.environ.get(
            "ZELOSMCP_BUILTIN_RESPONSE_FORMAT", "raw"
        )
    if rf not in RESPONSE_FORMATS:
        raise ConfigError(
            f"builtin.response_format must be one of "
            f"{sorted(RESPONSE_FORMATS)}, got {rf!r}"
        )

    compress = None
    compress_raw = raw.get("compress")
    if compress_raw is not None:
        compress = _parse_compress("builtin", compress_raw)

    sm = raw.get("strip_meta")
    if sm is None:
        _sm_env = os.environ.get("ZELOSMCP_STRIP_META")
        sm = (
            _sm_env.lower() not in ("0", "false", "no")
            if _sm_env is not None
            else True
        )
    if not isinstance(sm, bool):
        raise ConfigError("builtin.strip_meta must be a boolean")

    return BuiltinConfig(
        response_format=rf,
        strip_meta=sm,
        compress=compress,
    )


def parse_config(raw: Any) -> tuple[list[ServerSpec], str | None, BuiltinConfig]:
    """Parse a Cursor-style ``mcpServers`` payload.

    Args:
        raw: Decoded JSON object. Must contain ``mcpServers`` mapping
            name → server config. May also contain ``primaryMCP``
            and ``builtin``.

    Returns:
        ``(specs, primary_name, builtin_config)`` — order of ``specs``
        matches insertion order of ``mcpServers``. ``primary_name`` is
        ``None`` if not specified. ``builtin_config`` defaults to
        :class:`BuiltinConfig` defaults when the key is absent.

    Raises:
        ConfigError: On any structural or value error.
    """
    if not isinstance(raw, dict):
        raise ConfigError("Config must be a JSON object")

    servers = raw.get("mcpServers")
    if servers is None:
        raise ConfigError("Config must contain 'mcpServers'")
    if not isinstance(servers, dict):
        raise ConfigError("'mcpServers' must be an object mapping name → server config")
    if not servers:
        raise ConfigError("'mcpServers' must contain at least one server")

    specs: list[ServerSpec] = []
    seen: set[str] = set()
    for name, entry in servers.items():
        _validate_name(name)
        lower = name.lower()
        if lower in seen:
            raise ConfigError(f"Duplicate server name (case-insensitive): '{name}'")
        seen.add(lower)
        specs.append(_parse_server(name, entry))

    _check_mount_overlap(specs)

    primary = raw.get("primaryMCP")
    if primary is not None and not isinstance(primary, str):
        raise ConfigError("'primaryMCP' must be a string")
    # NOTE: primaryMCP is deprecated as of v0.3 — `/mcp` always aggregates every
    # running server. We still parse the field so old pasted configs keep working;
    # ProxyManager.start_all logs a deprecation warning when it sees a value.

    builtin_cfg = _parse_builtin_config(raw.get("builtin"))

    return specs, primary, builtin_cfg
