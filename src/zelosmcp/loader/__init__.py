"""Subagent loader: roster + artifact (skills + hooks) bundling (#27).

Two concerns live here:

1. **Roster** — the static set of subagents exposed as sync MCP tools
   (``Plan`` / ``Explore`` / ``general-purpose``), each described by a
   :class:`SubagentMeta`. This was the v0.3 stub (#24) and is unchanged.

2. **Artifact loader** (#27) — at invoke time a subagent's *bundle* of skills
   + hooks is loaded from the filesystem artifact store
   (:mod:`zelosmcp.loader.store`) and assembled into an
   :class:`~zelosmcp.loader.apply.AppliedBundle`
   (:mod:`zelosmcp.loader.apply`) handed to the subagent at spawn. Without
   this the EA platform is fixed-roster only: every subagent launches bare.

The :func:`load_subagent_bundle` convenience ties the two halves together —
given a subagent name it returns the applied bundle (or ``None`` when the
subagent has no manifest). It is the single call
:mod:`zelosmcp.tools.sync_subagent` makes at invoke time, before opening the
broker sync channel.
"""

from __future__ import annotations

from dataclasses import dataclass

from zelosmcp.loader.apply import AppliedBundle, apply_bundle
from zelosmcp.loader.store import (
    ArtifactStore,
    BundleManifestError,
    HookArtifact,
    SkillArtifact,
    SubagentBundle,
    resolve_artifacts_dir,
)

__all__ = [
    "SubagentMeta",
    "list_subagents",
    "get_subagent",
    "get_subagent_by_name",
    "load_subagent_bundle",
    "AppliedBundle",
    "apply_bundle",
    "ArtifactStore",
    "BundleManifestError",
    "HookArtifact",
    "SkillArtifact",
    "SubagentBundle",
    "resolve_artifacts_dir",
]


@dataclass(frozen=True)
class SubagentMeta:
    """Metadata describing one subagent exposed as a sync MCP tool.

    ``name`` is the broker-facing subagent type (sent in the sync-channel
    ``open`` request and echoed in the staged ``open`` frame). ``tool_name`` is
    the MCP tool name surfaced to the IDE. ``description`` is the tool
    description.
    """

    name: str
    tool_name: str
    description: str


# Static roster. Tool names are lowercased + suffixed so they read as verbs in
# the IDE tool list (``plan`` / ``explore`` / ``general_purpose``).
_ROSTER: tuple[SubagentMeta, ...] = (
    SubagentMeta(
        name="Plan",
        tool_name="plan",
        description=(
            "Run the Plan subagent synchronously over a broker sync channel. "
            "Streams the subagent's turns (token deltas, tool calls, tool "
            "results) back to the IDE and returns the consolidated transcript. "
            "Use to produce an implementation plan for a task before editing."
        ),
    ),
    SubagentMeta(
        name="Explore",
        tool_name="explore",
        description=(
            "Run the Explore subagent synchronously over a broker sync "
            "channel. Streams turns back to the IDE and returns the "
            "consolidated transcript. Use to investigate a codebase / answer "
            "a research question across many files."
        ),
    ),
    SubagentMeta(
        name="general-purpose",
        tool_name="general_purpose",
        description=(
            "Run the general-purpose subagent synchronously over a broker "
            "sync channel. Streams turns back to the IDE and returns the "
            "consolidated transcript. Use for open-ended multi-step tasks."
        ),
    ),
)

_BY_TOOL_NAME: dict[str, SubagentMeta] = {m.tool_name: m for m in _ROSTER}
_BY_NAME: dict[str, SubagentMeta] = {m.name: m for m in _ROSTER}


def list_subagents() -> tuple[SubagentMeta, ...]:
    """Return the static subagent roster."""
    return _ROSTER


def get_subagent(tool_name: str) -> SubagentMeta | None:
    """Look up a subagent by its MCP tool name (e.g. ``"plan"``)."""
    return _BY_TOOL_NAME.get(tool_name)


def get_subagent_by_name(name: str) -> SubagentMeta | None:
    """Look up a subagent by its broker subagent type (e.g. ``"Plan"``)."""
    return _BY_NAME.get(name)


def load_subagent_bundle(
    subagent: str,
    *,
    store: ArtifactStore | None = None,
) -> AppliedBundle | None:
    """Load + apply ``subagent``'s skill/hook bundle for an invocation.

    The single invoke-time entry point: reads the subagent's manifest from the
    artifact store (default: :class:`ArtifactStore` rooted at
    ``$ZELOS_ARTIFACTS_DIR``) and assembles it into an
    :class:`~zelosmcp.loader.apply.AppliedBundle`.

    Returns ``None`` when the subagent has no manifest (it launches bare, as
    pre-#27). Raises :class:`BundleManifestError` when a manifest exists but is
    malformed or references a missing artifact — fail-fast, *before* the broker
    channel is opened.
    """
    art_store = store if store is not None else ArtifactStore()
    bundle = art_store.load_bundle(subagent)
    return apply_bundle(bundle)
