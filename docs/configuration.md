# Configuration

zelosMCP accepts the same `mcpServers` JSON shape Cursor uses in its own `mcp.json`. No extra fields are required. This page covers the schema, the three transport flavors, reserved names, and the `/api/start` lifecycle.

## Where the config goes

The config is **POST**-ed to `/api/start` — it's not a config file zelosMCP reads from disk on startup. There are two common ways to send it:

1. **Web UI** (`http://localhost:8000`) — paste into the Configuration textarea, click START.
2. **`make load`** — POSTs the file at `$(ZELOSMCP_CONFIG)` (default: [`configs/default-zelosmcp.json`](../configs/default-zelosmcp.json)).
3. **`curl`**:

   ```bash
   curl -sS -X POST http://localhost:8000/api/start \
     -H 'Content-Type: application/json' \
     --data-binary @path/to/your-config.json
   ```

The state lives in memory. Restart the zelosMCP container and you'll need to re-POST.

## Schema

Top-level shape:

```json
{
  "mcpServers": {
    "<name>": { ... },
    "<name>": { ... }
  }
}
```

`<name>` becomes the URL segment for that backend's raw passthrough endpoint (`/<name>/mcp`) and the prefix Cursor sees on aggregated tool names (`<name>__<tool>`). Names must match `[A-Za-z0-9][A-Za-z0-9_\-.]*` and not collide with [reserved names](#reserved-names).

Per-server fields are discriminated by which keys are present:

### Stdio (`command`)

For an MCP server you spawn as a subprocess. Discriminated by the **presence of `command`**.

```json
{
  "filesystem": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/user_data_rw"],
    "env": { "DEBUG": "true" },
    "cwd": "/user_data_rw"
  }
}
```

| Field | Required | Type | Notes |
|---|---|---|---|
| `command` | yes | string | Executable. Searched on `PATH` inside the zelosMCP container. |
| `args` | no | array of strings | Arguments. Default `[]`. |
| `env` | no | object of strings | Extra env vars merged into the subprocess's environment. |
| `cwd` | no | string | Working directory for the subprocess. |

### SSE (`type: "sse"`)

For a remote MCP server speaking Server-Sent-Events. Discriminated by `type`.

```json
{
  "linear": {
    "type": "sse",
    "url": "https://mcp.linear.app/sse",
    "headers": { "Authorization": "Bearer ..." }
  }
}
```

| Field | Required | Type | Notes |
|---|---|---|---|
| `type` | yes | `"sse"` | Discriminator. |
| `url` | yes | string | Full URL of the SSE endpoint. |
| `headers` | no | object of strings | Sent on the SSE connection. |

### Streamable HTTP (`type: "streamable-http"`)

For a remote MCP server speaking the streamable-HTTP transport (the same transport zelosMCP itself uses).

```json
{
  "github": {
    "type": "streamable-http",
    "url": "https://api.githubcopilot.com/mcp/",
    "headers": { "Authorization": "Bearer $GITHUB_TOKEN" }
  }
}
```

| Field | Required | Type | Notes |
|---|---|---|---|
| `type` | yes | `"streamable-http"` | Discriminator. |
| `url` | yes | string | Full URL of the MCP endpoint. |
| `headers` | no | object of strings | Sent on every request. |

#### Zelos operator console (`/api/mcp`)

The Zelos operator console (`zelosctl web`) co-serves an MCP endpoint at **`/api/mcp`** — one tool
per verb of every installed `zelos.*` collection (`proxmox__host_prep`, `kubernetes__platform`,
`foundry__environment`, …) plus `zelos__*` job/docs tools and `zelos://` resources. It's a
`streamable-http` backend that authenticates like the console's REST API (a `Bearer` token in
`local-token` mode, or the oauth2-proxy/Dex identity headers in `oidc-proxy` mode — put zelosMCP
behind the same gate). Front it with:

```json
{
  "mcpServers": {
    "zelos": {
      "type": "streamable-http",
      "url": "https://console.<bed>.<product>.<domain>/api/mcp",
      "headers": { "Authorization": "Bearer $ZELOS_WEB_TOKEN" }
    }
  }
}
```

Ready-to-load example: [`configs/example-zelos-console.json`](../configs/example-zelos-console.json)
(`make load ZELOSMCP_CONFIG=configs/example-zelos-console.json`). The backend name (`zelos`) is the
tool prefix Cursor sees — `zelos__proxmox__host_prep`.

### OAuth passthrough (`passthrough`)

Some remote MCP servers — GitHub MCP at `api.githubcopilot.com/mcp`, Atlassian's hosted MCP, etc. — require **OAuth** rather than a static token. With `"passthrough": true`, zelosMCP forwards traffic transparently and lets the MCP client (Cursor) perform the OAuth dance directly with the upstream issuer. zelosMCP holds **no** OAuth state of its own; tokens flow through unchanged.

```json
{
  "github": {
    "type": "streamable-http",
    "url": "https://api.githubcopilot.com/mcp/",
    "passthrough": true,
    "passthroughPool": {
      "maxSessions": 64,
      "idleTtlSeconds": 1800
    }
  }
}
```

| Field | Required | Type | Default | Notes |
|---|---|---|---|---|
| `passthrough` | no | bool | `false` | Enables OAuth-passthrough mode. Only valid for `type: "streamable-http"`. |
| `auth.provider` | no | string | _(none)_ | **Modern broker mode**: name of an entry in [configs/auth-providers.json](#auth-providers-config). zelosMCP mints tokens via the provider (typically through the GUI Connections flow) and gates the backend's wrappers until the user authenticates. Mutually exclusive with `auth.bearer`. |
| `auth.audience` | no | string | _(none)_ | Provider-specific audience claim. Only valid alongside `auth.provider`. Reserved for future token-exchange providers; current device-flow providers ignore it. |
| `auth.bearer` | no | string | _(none)_ | **Legacy** static fallback token. Injected on outbound requests only when the inbound request has no `Authorization` header. Useful for headless / CI runs. Supports `${ENV_VAR}` interpolation. Prefer `auth.provider` -> a `static`-type provider for new configs. |
| `passthroughPool.maxSessions` | no | int | `64` | LRU cap on cached upstream sessions per backend. Each unique inbound `Authorization` value gets its own session (keyed by SHA-256 hash). |
| `passthroughPool.idleTtlSeconds` | no | int | `1800` | Idle TTL after which a cached upstream session is closed. |

How it works:

- **`/<name>/mcp`** — streaming reverse-proxy of MCP traffic. zelosMCP does not own a session here; each Cursor connection drives its own OAuth flow with the upstream. 401 + `WWW-Authenticate` from the upstream propagates verbatim. Useful when you want raw upstream tool names (no `<backend>__` prefix).
- **`/mcp` (aggregator)** — passthrough backends are auto-compressed to the standard non-`max` wrapper trio (`<name>__get_tool_schema`, `<name>__search_tools`, `<name>__invoke_tool`). The aggregator emits the wrappers in `tools/list` regardless of inbound auth state — pre-OAuth they carry an "auth required" description block; post-OAuth they include the real upstream catalog. The first wrapper invocation opens a per-Cursor upstream session; if the upstream returns 401, zelosMCP rewrites the response to `HTTP 401 + WWW-Authenticate` (the upstream's own challenge, so Cursor's OAuth client follows the canonical issuer). Subsequent calls dispatch through the standard `handle_compressed_call` path.

Constraints:

- **Compression auto-applies to passthrough backends** with `level=medium, scope=aggregator`. To opt out and revert to the previous "invisible until auth" behaviour, set `"compress": null`.
- **`compress.scope: "global"` is rejected.** The `/<name>/mcp` route is a streaming reverse proxy that doesn't terminate MCP, so the per-backend session manager can't host wrappers there. Use the default `aggregator` scope, or `catalog` for docs-only mode.
- **stdio and `type: "sse"` are rejected.** Passthrough is `type: "streamable-http"` only.
- **`tools/list` always succeeds.** The aggregator never blocks on auth — the wrappers are emitted regardless, and OAuth is driven by the first wrapper invocation rather than by `tools/list` itself.

See [oauth-passthrough.md](oauth-passthrough.md) for the full reference, sequence diagrams, troubleshooting, and an end-to-end walkthrough using GitHub MCP.

### Auth-providers config

In broker mode (`auth.provider` set on a backend), the provider definitions live in a **separate file** from the mcpServers catalog. Default location: [`configs/auth-providers.json`](../configs/auth-providers.json), overridable via `ZELOSMCP_AUTH_PROVIDERS_FILE`.

Top-level shape:

```json
{
  "providers": {
    "<name>": { "type": "...", ... }
  }
}
```

Why two files: different sensitivity classes. The mcpServers catalog (URLs, command lines) is low-sensitivity and ships as a Kubernetes ConfigMap; the providers config (env-resolved client_ids, plus the encryption key for the per-user token store) is medium-sensitivity and ships as a Secret.

Provider types:

- `github_device_flow` — public OAuth client against `github.com/login/device`. Required: `client_id`. Optional: `scopes` (list of strings), `membership_hint` (UX-only display string).
- `okta_authorization_code` — public OAuth Native app against an Okta tenant using Authorization Code + PKCE. Required: `client_id`, `issuer` (`https://<okta-domain>/oauth2/<auth-server-id>`). Optional: `redirect_uri` (defaults to `http://localhost:8000/api/auth/<provider>/callback`), `scopes`, `membership_hint`.
- `okta_device_flow` — public OAuth client against an Okta tenant using Device Authorization Grant. Required: `client_id`, `issuer`. Optional: `scopes`, `membership_hint`. Use only if your Okta admin enables the Device Authorization grant.
- `passthrough` — wraps the legacy "forward Authorization verbatim" behaviour as an `AuthProvider`. No additional fields.
- `static` — wraps a configured bearer token (env-interpolated). Required: `bearer`.

The `membership_hint` field on any provider is purely for UX — the GUI Connections card surfaces it as `"Membership required: <hint>"` so users know which authorized-group / role they need before clicking Connect. Never used for authorization (the upstream IdP enforces).

Live edits via the API:

```bash
curl -sS -X POST http://localhost:8000/api/auth/providers/config \
  -H 'Content-Type: application/json' \
  --data-binary @configs/auth-providers.json
```

`GET /api/auth/providers/config` returns the currently-loaded providers with secrets redacted to `***`. See [oauth-passthrough.md](oauth-passthrough.md#configuration-two-files) for the full schema reference.

### Reverse-proxy (`reverseProxy`)

Any backend (stdio or remote) can declare an optional `reverseProxy` block alongside the transport fields above. When set, zelosMCP forwards HTTP requests on the configured `mount` path to the backend's HTTP sidecar — letting you expose a dashboard or REST API through zelosMCP's port without leaking the backend's own port.

```json
"pincher": {
  "command": "pincher",
  "args": ["--data-dir", "/tmp/pincher", "--http", "127.0.0.1:8080", "--trust-proxy"],
  "reverseProxy": {
    "mount": "/pincher",
    "upstream": "http://127.0.0.1:8080",
    "openapi": {
      "path": "/v1/openapi.json"
    }
  }
}
```

| Field | Required | Type | Notes |
|---|---|---|---|
| `mount` | yes | string | URL prefix on zelosMCP, e.g. `/pincher`. Must start with `/`, no trailing `/`. Cannot collide with reserved mounts. |
| `upstream` | yes | string | Backend HTTP sidecar URL. Recommend a loopback host. |
| `stripPrefix` | no | bool | Strip `mount` from the path before forwarding. Default `false`. |
| `headers` | no | object of strings | Extra request headers. Override auto-injected `X-Forwarded-*`. |
| `auth.bearer` | no | string | Bearer token to inject when caller has no `Authorization`. Supports `${ENV_VAR}` interpolation. |
| `openapi.path` | no | string | Upstream OpenAPI contract path, relative to `upstream` and starting with `/`. When present and the backend is running, zelosMCP merges those endpoints into `/openapi.json` under the reverse-proxy `mount`, so they appear in `/docs`. |

See [reverse-proxy.md](reverse-proxy.md) for the full reference, including the canonical `X-Forwarded-*` set, network-isolation pattern, and pincher worked example.

### Compression (`compress`)

Every backend is compressed by default — zelosMCP automatically swaps each backend's full tool surface (N tools, each with descriptions and JSON schemas) for a small wrapper trio on the aggregator at `/mcp` (`<backend>__get_tool_schema`, `<backend>__search_tools`, and `<backend>__invoke_tool`), slashing tokens spent on `tools/list`. Wrappers stay invocable; the agent can search compressed catalog lines via `search_tools(query, limit?)`, fetch a tool's full schema on demand via `get_tool_schema(tool_name)`, and run it via `invoke_tool(tool_name, tool_input)`. At `level=max`, the wrapper surface is instead a single `list_tools` wrapper. Add a `compress` block only when you want to override the default level/scope, or set `"compress": null` to opt the backend out entirely.

```json
"kubernetes": {
  "command": "npx",
  "args": ["-y", "kubernetes-mcp-server@latest"],
  "compress": {
    "level": "medium",
    "scope": "aggregator"
  }
}
```

| Field | Required | Type | Default | Notes |
|---|---|---|---|---|
| `level` | no | string | `"medium"` | One of `"low"`, `"medium"`, `"high"`, `"max"`. `low` keeps full descriptions; `max` collapses to a single `list_tools` wrapper. |
| `scope` | no | string | `"aggregator"` | One of `"catalog"`, `"aggregator"`, `"global"`. Controls which endpoints surface the wrappers vs. the full tool list. |

Quick scope reference:

| `scope` | `/<name>/mcp` | `/mcp` (aggregator) | docs / cursor-rule |
|---|---|---|---|
| `catalog` | full | full | compressed |
| `aggregator` (default) | full | wrappers | compressed |
| `global` | wrappers | wrappers | compressed |

See [compression.md](compression.md) for the full reference, level comparison, agent-side flow, and worked examples.

## Reserved names

A handful of `<name>` values collide with built-in HTTP routes; zelosMCP rejects them with a `ConfigError`:

| Name | Why |
|---|---|
| `mcp` | Aggregator endpoint at `/mcp`. |
| `api` | Control plane at `/api/*`. |
| `docs`, `redoc`, `openapi.json`, `openapi` | API docs at `/docs`, `/redoc`, `/openapi.json`. |
| `static` | Reserved for future static-asset routes. |
| `zelosmcp` | The always-on built-in MCP at `/zelosmcp/mcp`. |

Names are matched case-insensitively. Pick a different name (e.g. `local-tools` instead of `zelosmcp`) if you really need that string.

## Deprecated `primaryMCP`

Older configs include a top-level `primaryMCP` field naming a single backend to mirror at `/mcp`. As of v0.3, `/mcp` always aggregates every running server, so `primaryMCP` is parsed for backward compatibility but ignored — a one-line deprecation warning is logged when present. Drop it from new configs.

## Lifecycle

`POST /api/start` runs:

```mermaid
sequenceDiagram
  participant client
  participant api as POST /api/start
  participant mgr as ProxyManager
  participant agg as Aggregator
  client->>api: { mcpServers: {...} }
  api->>mgr: stop_all()
  Note over mgr: Stops user backends + aggregator.<br/>Built-in `zelosmcp` survives.
  api->>mgr: parse_config()
  alt validation fails
    mgr-->>api: ConfigError
    api-->>client: 400 + error message
  else
    api->>mgr: start each ProxyState in parallel
    mgr->>agg: start() (after backends are up)
    mgr-->>api: per-server result map
    api-->>client: 200 + {ok, primary, servers: {<name>: {ok, error?}}}
  end
```

Per-backend failures don't abort the whole request — each is reported in the per-server result map. The aggregator still starts as long as **any** backend is up; you can fix the broken entries and re-POST.

## Per-server lifecycle endpoints

Beyond bulk replace via `/api/start`, individual backends can be toggled:

| Endpoint | Effect |
|---|---|
| `POST /api/servers/<name>/start` | Start a single (already-configured) backend. |
| `POST /api/servers/<name>/stop` | Stop a single backend without affecting the others. |
| `GET /api/servers/<name>` | One-server slice of `/api/status`. |

These all refuse `<name> == "zelosmcp"` (the built-in is always-on and not toggleable).

## Common errors

| Error message | Cause |
|---|---|
| `Server name '<x>' is reserved (collides with a built-in route)` | Pick a different name. |
| `Server name '<x>' is invalid: use letters, digits, '-', '_', or '.'` | Naming pattern violation. |
| `Server '<x>': could not determine transport. Provide 'command' (stdio) or 'type' set to 'sse' or 'streamable-http'.` | Missing `command` and `type` keys. |
| `Server '<x>': '<field>' must be ...` | Type mismatch on a field. |
| `Duplicate server name (case-insensitive): '<x>'` | Two entries with names that case-fold to the same string. |

All of these come back as 400-status JSON: `{"ok": false, "error": "<message>"}`.

## See also

- [default-mcps.md](default-mcps.md) — what the default backends do (mandatory `pincher` and `filesystem`; default-config `kubernetes` and `docker`).
- [repositories.md](repositories.md) — Repositories UI panel and `/api/repos*` endpoints (write rules into discovered repos, index them in pincher).
- [reverse-proxy.md](reverse-proxy.md) — full reference for the optional `reverseProxy` block.
- [compression.md](compression.md) — full reference for the optional `compress` block.
- [http-api.md](http-api.md) — full HTTP API reference for `/api/start` and friends.
- [makefile.md](makefile.md) — `make load ZELOSMCP_CONFIG=...` to push your own config.
