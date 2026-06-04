"""Async-task MCP tool: ``submit_inference_task`` (#25).

Routes an inference request through zelosmcp instead of straight to the
gateway (the v1 surface in ``zelosai/docs/architecture/00-overview.md``). On
invoke the tool:

1. Issues per-invocation broker + backplane bearer tokens for the
   gateway-propagated caller (#26).
2. Opens a broker share for the caller's workspace so the remote zelosclient
   worker can mount it (#23).
3. Builds a backplane request envelope carrying the share coords + the
   inference body (``prompt`` / ``model`` / ``params``), validates it against
   the canonical schema, and publishes it onto ``inference.requests.<kind>``
   over NATS (#25).
4. Returns ``{id, replyTopic}`` so the caller can correlate the eventual reply
   (the response envelope echoes ``id`` as ``corrId`` on ``replyTopic``).

The core (:func:`submit_inference_task`) is dependency-injected (broker client,
publisher, identity, signing key) so it is unit-testable against a fake broker
+ embedded / fake NATS without live infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from zelosmcp.auth import middleware as auth_mw
from zelosmcp.auth.identity import CallerIdentity
from zelosmcp.backplane.envelope import RequestEnvelope, ShareCoordinates
from zelosmcp.backplane.publisher import BackplanePublisher
from zelosmcp.broker.client import BrokerClient
from zelosmcp.broker.schema import MOUNT_WEBDAV

# Default request kind when the caller doesn't pin one. ``codegen`` is the
# primary EA inference class (topics.yaml: ``inference.requests.codegen``).
DEFAULT_KIND = "codegen"

# The MCP tool spec surfaced to the IDE. Kept as a plain dict so the data-path
# server can build an ``mcp.types.Tool`` from it without importing mcp here.
ASYNC_TASK_TOOL: dict[str, Any] = {
    "name": "submit_inference_task",
    "description": (
        "Submit an asynchronous inference task to the Zelos backplane. Opens "
        "a workspace share so the remote worker can mount your files, "
        "publishes a request envelope onto inference.requests.<kind>, and "
        "returns {id, replyTopic}. Poll/subscribe replyTopic for the result "
        "(correlated by id). Use for long-running generation/analysis that "
        "should not block the IDE."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "The inference prompt."},
            "model": {
                "type": "string",
                "description": "Target model identifier (worker-specific).",
            },
            "params": {
                "type": "object",
                "description": "Optional model parameters (temperature, max_tokens, ...).",
            },
            "kind": {
                "type": "string",
                "description": (
                    "Inference class / topic suffix (e.g. 'codegen', "
                    "'analysis'). Defaults to 'codegen'."
                ),
            },
            "workspace_path": {
                "type": "string",
                "description": "Path to share with the worker (optional).",
            },
        },
        "required": ["prompt"],
        "additionalProperties": False,
    },
}


@dataclass
class AsyncTaskDeps:
    """Injected collaborators for :func:`submit_inference_task`.

    ``broker`` opens the workspace share; ``publisher`` publishes the envelope.
    ``identity`` / ``signing_key`` override the ContextVar-resolved caller and
    env signing key (tests). ``open_share`` can be disabled for callers that
    have no workspace to mount.
    """

    broker: BrokerClient
    publisher: BackplanePublisher
    identity: CallerIdentity | None = None
    signing_key: str | None = None
    open_share: bool = True


async def submit_inference_task(
    *,
    prompt: str,
    deps: AsyncTaskDeps,
    model: str | None = None,
    params: dict[str, Any] | None = None,
    kind: str = DEFAULT_KIND,
    workspace_path: str | None = None,
    protocols: list[str] | None = None,
) -> dict[str, str]:
    """Open a share, publish an inference envelope, return ``{id, replyTopic}``.

    The share is intentionally *not* revoked here: the async worker mounts it
    after this call returns, so its lifetime is governed by the broker's
    ``ttl_seconds`` (and an eventual completion event), not by this tool.
    """
    identity = deps.identity if deps.identity is not None else auth_mw.resolve_identity()
    broker_auth = "Bearer " + auth_mw.issue_broker_token(
        identity=identity, signing_key=deps.signing_key
    )
    backplane_token = auth_mw.issue_backplane_token(
        identity=identity, signing_key=deps.signing_key
    )

    share_coords: ShareCoordinates | None = None
    if deps.open_share:
        share = await deps.broker.create_share(
            protocols=protocols or [MOUNT_WEBDAV],
            workspace_path=workspace_path,
            caller_id=identity.subject or None,
            auth_header=broker_auth,
        )
        proto = share.preferred_protocol()
        share_coords = ShareCoordinates(
            share_token=share.token,
            share_url=proto.url if proto is not None else "",
            share_protocol=proto.kind if proto is not None else None,
            mount_hint=share.mount_hint,
        )

    body: dict[str, Any] = {"prompt": prompt}
    if model is not None:
        body["model"] = model
    if params is not None:
        body["params"] = params

    envelope = RequestEnvelope.build(
        kind=kind,
        payload=body,
        share=share_coords,
        trace_id=identity.subject or None,
    )
    return await deps.publisher.publish_request(envelope, token=backplane_token)
