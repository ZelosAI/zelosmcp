"""Sync-subagent MCP tools (#24).

Exposes each subagent (``Plan`` / ``Explore`` / ``general-purpose``, from
:mod:`zelosmcp.loader`) as an MCP tool. When invoked, a tool:

1. Issues a per-invocation broker bearer token for the gateway-propagated
   caller (#26).
2. Optionally opens a broker share for the caller's workspace so the subagent
   can mount it, then opens a broker sync channel bound to that share +
   subagent type (#23).
3. Attaches the sync-channel WebSocket, sends a ``turn`` request frame, and
   relays the subagent's streamed frames (``open`` → ``turn`` → ``token``+ →
   ``tool_call`` / ``tool_result`` → ``turn_end``) back to the IDE.
4. Tears the channel + share down and returns the consolidated transcript.

The core (:func:`run_sync_subagent`) is dependency-injected (broker client,
sync-channel factory, identity, signing key) so it is fully unit-testable
against a fake broker without a live WebSocket. The MCP-handler glue lives in
:mod:`zelosmcp.server`.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from zelosmcp.auth import middleware as auth_mw
from zelosmcp.auth.identity import CallerIdentity
from zelosmcp.broker.client import BrokerClient
from zelosmcp.broker.schema import (
    KIND_TOKEN,
    KIND_TOOL_CALL,
    KIND_TOOL_RESULT,
    KIND_TURN,
    KIND_TURN_END,
    SyncChannel,
)
from zelosmcp.broker.sync_channel import SyncChannelClient
from zelosmcp.loader import (
    AppliedBundle,
    SubagentMeta,
    list_subagents,
    load_subagent_bundle,
)

# A factory that builds an attached-capable sync-channel client for an
# ``attach_url`` + bearer header. Injectable for tests.
ChannelFactory = Callable[[str, str | None], SyncChannelClient]

# Optional sink invoked for each relayed frame so the MCP layer can forward
# streaming progress to the IDE. Receives the decoded frame model.
FrameSink = Callable[[Any], Awaitable[None]]

# Loads + applies a subagent's skill/hook bundle at invoke time (#27). Returns
# the assembled bundle, or ``None`` when the subagent has no manifest (bare
# launch). Injectable so tests don't touch the filesystem; defaults to the
# filesystem artifact store via :func:`zelosmcp.loader.load_subagent_bundle`.
BundleLoader = Callable[[str], "AppliedBundle | None"]


def _default_channel_factory(attach_url: str, auth_header: str | None) -> SyncChannelClient:
    return SyncChannelClient(attach_url, auth_header=auth_header)


@dataclass
class SubagentDeps:
    """Injected collaborators for :func:`run_sync_subagent`.

    ``broker`` is a connected :class:`BrokerClient`. ``channel_factory`` builds
    the WS client (defaults to the real one). ``identity`` overrides the
    ContextVar-resolved caller (tests). ``signing_key`` overrides the env
    signing key (tests). ``frame_sink`` receives each relayed frame for
    streaming to the IDE. ``bundle_loader`` loads the subagent's skill/hook
    bundle at invoke time (#27); defaults to the filesystem artifact store.
    """

    broker: BrokerClient
    channel_factory: ChannelFactory = _default_channel_factory
    identity: CallerIdentity | None = None
    signing_key: str | None = None
    frame_sink: FrameSink | None = None
    bundle_loader: BundleLoader = load_subagent_bundle


@dataclass
class SyncSubagentResult:
    """Outcome of a sync-subagent run.

    ``transcript`` is the consolidated turn-end message; ``frames`` is the full
    ordered list of relayed frames (as wire dicts) for the caller's
    convenience; ``usage`` is the turn-end usage block if present. ``bundle``
    is the compact reference of the skill/hook bundle loaded for this
    invocation (#27), or ``None`` when the subagent launched bare.
    """

    session_id: str
    subagent: str
    transcript: Any | None = None
    usage: Any | None = None
    frames: list[dict[str, Any]] = field(default_factory=list)
    bundle: dict[str, Any] | None = None


def subagent_tool_specs() -> list[SubagentMeta]:
    """Return the subagent roster to expose as MCP tools (one per subagent)."""
    return list(list_subagents())


def _share_token(share: Any) -> str | None:
    return getattr(share, "token", None)


async def run_sync_subagent(
    subagent: SubagentMeta,
    *,
    prompt: str,
    deps: SubagentDeps,
    workspace_path: str | None = None,
    protocols: list[str] | None = None,
    open_share: bool = True,
) -> SyncSubagentResult:
    """Drive one synchronous subagent turn over a broker sync channel.

    Lifecycle: load skill/hook bundle (#27) → (optional) create share → open
    sync channel (carrying the bundle reference) → attach WS → send ``turn``
    (with skill fragments as first-turn context) → relay frames → close channel
    → revoke share. The bundle is loaded *first* so a malformed manifest fails
    fast before any broker resource is allocated. Cleanup runs even if the relay
    raises. Returns the consolidated transcript from the ``turn_end`` frame.
    """
    identity = deps.identity if deps.identity is not None else auth_mw.resolve_identity()
    auth_header = "Bearer " + auth_mw.issue_broker_token(
        identity=identity, signing_key=deps.signing_key
    )

    # Load + apply the subagent's skill/hook bundle (#27) before allocating any
    # broker resource. A malformed manifest raises here (BundleManifestError) so
    # the failure is loud and pre-flight, never mid-turn.
    applied: AppliedBundle | None = deps.bundle_loader(subagent.name)
    open_bundle = applied.open_payload() if applied is not None else None

    share = None
    channel: SyncChannel | None = None
    share_token: str | None = None
    try:
        if open_share:
            share = await deps.broker.create_share(
                protocols=protocols or ["webdav"],
                workspace_path=workspace_path,
                caller_id=identity.subject or None,
                auth_header=auth_header,
            )
            share_token = _share_token(share)

        channel = await deps.broker.open_sync_channel(
            subagent=subagent.name,
            share=share_token,
            bundle=open_bundle,
            auth_header=auth_header,
        )
        attach_url = deps.broker.attach_ws_url(channel)
        ws = deps.channel_factory(attach_url, auth_header)

        result = SyncSubagentResult(
            session_id=channel.session_id,
            subagent=subagent.name,
            bundle=open_bundle,
        )
        async with ws:
            # Request the turn. The broker has already staged an ``open``
            # frame; sending a ``turn`` frame carrying the prompt kicks off
            # the subagent. ``content`` is opaque JSON the broker relays. When
            # a bundle is active its skill fragments ride along as first-turn
            # ``context`` so the skill knowledge (incl. each skill's
            # description) is in the subagent's context at spawn (#27).
            content: dict[str, Any] = {"role": "user", "text": prompt}
            if applied is not None and applied.system_prompt_fragments:
                content["context"] = applied.system_prompt()
            await ws.send(
                {
                    "kind": KIND_TURN,
                    "session_id": channel.session_id,
                    "content": content,
                }
            )
            async for frame in ws.frames():
                wire = frame.model_dump(exclude_none=True)
                result.frames.append(wire)
                if deps.frame_sink is not None:
                    await deps.frame_sink(frame)
                kind = getattr(frame, "kind", None)
                if kind == KIND_TURN_END:
                    result.transcript = getattr(frame, "message", None)
                    result.usage = getattr(frame, "usage", None)
        return result
    finally:
        # Teardown is best-effort: a failure here must not mask the turn
        # result (or a relay error already propagating).
        if channel is not None:
            with contextlib.suppress(Exception):
                await deps.broker.close_sync_channel(
                    channel.session_id, reason="completed", auth_header=auth_header
                )
        if share_token is not None:
            with contextlib.suppress(Exception):
                await deps.broker.revoke_share(
                    share_token, reason="completed", auth_header=auth_header
                )


# Frame kinds the relay forwards to the IDE as streaming progress. Exported so
# the MCP server layer and tests share one definition.
STREAMED_KINDS = frozenset(
    {KIND_TURN, KIND_TOKEN, KIND_TOOL_CALL, KIND_TOOL_RESULT, KIND_TURN_END}
)
